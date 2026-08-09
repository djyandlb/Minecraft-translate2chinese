# -*- coding: utf-8 -*-
"""A5 统一全自动翻译流程（B 阶段：全文本覆盖 + 硬编码 AI 自动判断）。

一个入口跑完全部：拖入整合包 / mod jar / 地图 → 自动识别 → 全文本覆盖
（语言文件 + 结构化 JSON + en_us 文本）+ 硬编码 AI 自动判断一起翻译
（共用引擎/记忆/状态机）→ 产物 = 资源包 zip + 汉化 jar 副本。
map 委托 maps_flow。原 jar/存档只读，一切写操作只在 work 副本。

引擎分流：
  - LLMClient：硬编码候选走 ai_judge_translate（LLM 判断是否用户可见并翻译）
  - MachineClient：无法 AI 判断，跳过硬编码（明确 warn），json/lines 文本覆盖照做
  - 其他兜底引擎（测试假引擎等）：硬编码逐条全翻（无 AI 判断）
"""
import asyncio
import json
import shutil
import zipfile
from pathlib import Path, PurePosixPath

from app.archive import extract_modpack, is_archive
from app.cleanup import cleanup_task_work
from app.config import AppConfig
from app.detect import (_HARDCODE_MAX_BYTES, detect_input_type, detect_source_lang,
                        infer_pack_format, needs_translation)
from app.diff import build_jobs
from app.glossary import load_glossary, term_inject_prompt
from app.hardcode import (ai_judge_translate, replace_hardcoded_strings,
                          scan_hardcoded_candidates)
from app.maps.flow import run_map_translation
from app.memory import MemoryStore
from app.models import AutoRequest, MapTranslateRequest
from app.resourcepack import build_resource_pack
from app.scanner import scan_jar, scan_modpack
from app.tasks import TaskStore
from app.text_sources import (TextSource, discover_pack_text_sources,
                              discover_text_sources, render_pack_source,
                              write_lang_into_jar, write_translated)
from app.translate.engine import create_engine
from app.vp import build_vp_module, download_vault_patcher, infer_modpack_runtime
from app.translate.han import is_same_script, simplify, traditional
from app.translate.llm import LLMClient
from app.translate.machine import MachineClient

# 汉化命名映射：target_lang → 显示名（zh_cn→简体中文、zh_tw→繁体中文，其他原样）
_LANG_NAMES = {"zh_cn": "简体中文", "zh_tw": "繁体中文"}


def lang_display_name(target_lang: str) -> str:
    """汉化命名映射：zh_cn→简体中文、zh_tw→繁体中文，其他 target_lang 原样。"""
    return _LANG_NAMES.get(target_lang, target_lang)


_PATCH_README = """【汉化补丁包使用说明】
把整个汉化补丁包解压到整合包根目录，即可覆盖生效（内部路径与整合包根目录精确对齐）：
  config/            → 整合包根目录的 config/
  data/              → 整合包根目录的 data/
  kubejs/            → 整合包根目录的 kubejs/
  scripts/           → 整合包根目录的 scripts/
  vault-patcher.jar  → 请移动到整合包 mods/ 目录（Vault Patcher 模组，已按版本自动下载）
  vaultpatcher/      → 游戏根目录（整合包根目录，VP 加载硬编码映射用）
模组语言文件汉化见「模组汉化资源包.zip」（放入游戏资源包目录）。
若补丁包内没有 vault-patcher.jar（自动下载失败/联网不可用），硬编码汉化会
以 hardcoded/ 目录的汉化 mod jar 形式提供（替换对应 mod）。
覆盖前建议先备份原文件。
"""


def _build_patch_pack(entries: list[tuple[str, str | bytes]], out_path: Path) -> None:
    """生成汉化补丁包 zip：相对路径条目（译文文本或字节，如 VP jar）+ 使用说明.txt。

    条目相对路径由整合包 rglob 相对路径天然生成；双保险白名单校验防穿越
    （不含 .. 与绝对路径）。原整合包只读，翻译内容在调用方渲染完成。
    """
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("使用说明.txt", _PATCH_README)
        for rel, content in entries:
            clean = PurePosixPath(rel)
            if clean.is_absolute() or ".." in clean.parts:
                continue
            zf.writestr(rel, content)


async def run_auto_translation(task_id: str, req: AutoRequest, cfg: AppConfig,
                               store: TaskStore, work_dir: Path, outputs_dir: Path) -> None:
    """统一全自动翻译：自动识别类型 → 全文本覆盖 + 硬编码 AI 判断并入 → 资源包 + 汉化 jar；map 委托。

    流程骨架照 translator.py / hardcode_flow.py：扫描 → 查记忆 → 引擎翻译
    （术语注入+简繁捷径+token统计）→ 资源包 + jar 副本改写。

    work_dir 为中间产物区（temp，任务终态后清理任务级子目录）；
    outputs_dir 为产物区（exe 旁 outputs/，资源包/汉化 jar 落这里）。
    """
    state = store.load(task_id)
    memory = MemoryStore(work_dir / "memory.json")
    glossary_prompt = term_inject_prompt(load_glossary(work_dir / "glossary.json"))
    try:
        path = Path(req.path)
        if is_archive(path):
            # 任务隔离解压目录，避免并发任务共用 extracted/ 互相覆盖（F4）
            path = extract_modpack(path, work_dir / "extracted" / task_id)
        kind = detect_input_type(path)
        if kind == "map":
            # 委托地图流程（源语言/版本由 maps_flow 处理），返回
            await run_map_translation(
                task_id,
                MapTranslateRequest(path=str(path),
                                    source_lang=req.source_lang or "en_us",
                                    target_lang=req.target_lang),
                cfg, store, work_dir, outputs_dir)
            return
        if kind == "unknown":
            state.status = "failed"
            state.progress.append({"status": "error",
                                   "error": "无法识别输入类型（支持整合包目录/压缩包、mod jar、地图）"})
            store.save(state)
            return
        if kind == "modjar" and not path.is_file():
            state.status = "failed"
            state.progress.append({"status": "error", "error": "mod jar 输入应为有效的 .jar 文件"})
            store.save(state)
            return

        # 聚 jar 列表（modpack: mods/**/*.jar；modjar: 该文件本身）
        jars = sorted((path / "mods").rglob("*.jar")) if kind == "modpack" else [path]
        if not jars:
            state.status = "failed"
            state.progress.append({"status": "error",
                                   "error": ("未在整合包中找到任何 mod jar（mods/ 下无 .jar）"
                                             if kind == "modpack" else "未找到可翻译的 jar")})
            store.save(state)
            return

        # 整合包目录文本源（任务线/config/data/kubejs）：只读扫描，译文进汉化补丁包
        pack_sources: list[TextSource] = []
        if kind == "modpack":
            try:
                pack_sources = discover_pack_text_sources(path)
            except Exception as e:
                state.progress.append({"status": "warn",
                                       "error": f"扫描整合包目录文本源失败：{e}"})

        # 源语言自动检测（req.source_lang 为空时）；全汉化返回 None → 兜底 en_us + warn
        auto_lang = detect_source_lang(jars, req.target_lang) if not req.source_lang else None
        if auto_lang is None and not req.source_lang:
            # 可继续：缺口为 0 自然空包，由下游空词条/无可导出分支覆盖
            state.progress.append({"status": "warn",
                                   "error": "源语言自动检测：所有资源已是目标语言，无空缺可翻（以 en_us 兜底继续）"})
        source_lang = req.source_lang or auto_lang or "en_us"

        # ① 语言文件扫描 → jobs（key 级跳过已汉化，支持任意源语言）
        scans = (scan_modpack(path, source_lang, req.target_lang, "mods") if kind == "modpack"
                 else scan_jar(path, source_lang, req.target_lang))
        jobs = build_jobs(scans)

        # 引擎创建（on_usage 必须在 create_engine 前定义，供回挂）
        def on_usage(t_in: int, t_out: int) -> None:
            state.tokens_in += t_in
            state.tokens_out += t_out
            store.save(state)

        engine = create_engine(cfg)
        if hasattr(engine, "on_usage"):
            engine.on_usage = on_usage
        if isinstance(engine, LLMClient) and glossary_prompt:
            engine.glossary_prompt = glossary_prompt
        if isinstance(engine, LLMClient) and not engine.api_key:
            # R1：keyring 空 key → 引擎主路径假成功，提前告警
            state.progress.append({"status": "warn",
                                   "error": "未配置 API Key，AI 翻译将失败，请在配置页填写"})

        engine_machine = isinstance(engine, MachineClient)

        # ② 全文本覆盖：结构化 JSON / en_us 文本（lines）→ 写回 jar 副本。
        #    语言文件（lang）已由 scan+jobs 走 by_mod 资源包，这里只收非 lang 源。
        text_sources_by_jar: dict[Path, list[TextSource]] = {}
        for jar in jars:
            try:
                srcs = [s for s in discover_text_sources(jar) if s.kind != "lang"]
                if srcs:
                    text_sources_by_jar[jar] = srcs
            except Exception as e:
                # 损坏 jar 异常兜底跳过，不让一个坏文件中断整包流程
                state.progress.append({"status": "warn",
                                       "error": f"扫描 {jar.name} 文本源失败：{e}"})

        # ③ 硬编码候选扫描（仅非 machine 引擎；LLM 走 AI 判断，兜底引擎走全翻）
        hard_candidates_by_jar: dict[Path, list[dict]] = {}
        if engine_machine:
            # 在线机翻无法 AI 判断「是否用户可见」，明确提示跳过
            state.progress.append({"status": "warn",
                                   "error": "在线机翻无法 AI 判断硬编码，已跳过硬编码翻译"})
        else:
            for jar in jars:
                try:
                    if jar.stat().st_size > _HARDCODE_MAX_BYTES:
                        state.progress.append({"status": "warn",
                                               "error": (f"跳过超大 jar {jar.name} 的硬编码扫描"
                                                         f"（>{_HARDCODE_MAX_BYTES // 1024 // 1024}MB）")})
                        continue
                    cands = scan_hardcoded_candidates(jar)
                    if cands:
                        hard_candidates_by_jar[jar] = cands
                except Exception as e:
                    # 损坏 jar 异常兜底跳过，不让一个坏文件中断整包流程
                    state.progress.append({"status": "warn",
                                           "error": f"扫描 {jar.name} 硬编码字符串失败：{e}"})

        # 进度总量：语言文件 jobs + jar 内 json/lines + 整合包目录文本源 + 硬编码候选（machine 不数硬编码）
        state.total = len(jobs) + sum(
            len(s.entries) for srcs in text_sources_by_jar.values() for s in srcs)
        state.total += sum(len(s.entries) for s in pack_sources)
        if not engine_machine:
            state.total += sum(len(c) for c in hard_candidates_by_jar.values())
        if state.total == 0:
            # 空词条：直接 done + warn，不导出空包
            state.status = "done"
            state.progress.append({"status": "warn",
                                   "error": "未发现可翻译的词条（语言文件缺口与文本源/硬编码都为空）"})
            store.save(state)
            return
        store.save(state)

        same_script = is_same_script(source_lang, req.target_lang)

        async def _wait_if_paused() -> None:
            """Y4：暂停等待也必须响应取消，否则取消被暂停卡死。"""
            while state.paused and not state.cancelled:
                await asyncio.sleep(0.5)

        async def _translate_batch_pipeline(items, translate_fn, batch_size: int = 20) -> None:
            """批量翻译流水线（语言文件 / json-lines / 兜底硬编码共用）。

            逐条预处理（已汉化跳过 / 记忆命中 / 简繁直转）在批外完成，只有
            真正需要走引擎的条目才收集成批；攒满 batch_size 一次 translate_batch，
            结果逐条写回记忆/产物/进度。批之间响应取消与暂停。

            items: 可迭代对象，元素为 {"key", "text", "sink"}（sink 为写回产物字典）；
            translate_fn: async (texts: list[str]) -> list[str]，批量走引擎。
            """
            pending: list[dict] = []          # 待引擎条目 {key, text, sink}

            async def _flush() -> None:
                """攒满一批 → 一次批量翻译 → 逐条写回记忆/产物/进度。"""
                if not pending:
                    return
                texts = [p["text"] for p in pending]
                try:
                    translated_list = await translate_fn(texts)
                except Exception:
                    # M6-recheck 批量版：整批引擎异常（网络/API 失败）→ 全部回原文 + 计 failed
                    translated_list = texts
                for p, translated in zip(pending, translated_list):
                    key, text, sink = p["key"], p["text"], p["sink"]
                    if translated == text:
                        # Y3：引擎失败回原文 → 计入 failed，前端醒目提示
                        state.failed += 1
                    memory.set(text, req.target_lang, translated)
                    sink[key] = translated
                    state.done += 1
                    state.progress.append({"key": key, "source": text,
                                           "translated": translated, "status": "done"})
                    if state.done % 10 == 0:
                        memory.save()
                        store.save(state)
                pending.clear()

            for item in items:
                if state.cancelled:
                    state.status = "cancelled"
                    store.save(state)
                    return
                await _wait_if_paused()
                key, text, sink = item["key"], item["text"], item["sink"]
                if not same_script and not needs_translation(text, req.target_lang):
                    # 已汉化（含 CJK）/ 技术串：跳过翻译，计 done，不入产物。
                    # 注意：same_script（简繁互转）时中文源文本必须保留翻译，跳过会漏转繁体。
                    state.done += 1
                    state.progress.append({"key": key, "source": text,
                                           "translated": text, "status": "done"})
                    continue
                cached = memory.get(text, req.target_lang)
                if cached:
                    # 记忆命中：直接写回，不走引擎
                    sink[key] = cached
                    state.done += 1
                    state.progress.append({"key": key, "source": text,
                                           "translated": cached, "status": "done"})
                    if state.done % 10 == 0:
                        memory.save()
                        store.save(state)
                    continue
                if same_script:
                    # 简繁双向直转，免 AI：zh_tw 走繁化，zh_cn 走简化（F5）
                    translated = (traditional(text) if req.target_lang == "zh_tw" else simplify(text))
                    memory.set(text, req.target_lang, translated)
                    sink[key] = translated
                    state.done += 1
                    state.progress.append({"key": key, "source": text,
                                           "translated": translated, "status": "done"})
                    if state.done % 10 == 0:
                        memory.save()
                        store.save(state)
                    continue
                # 需走引擎：收集入批，攒满 batch_size 一次性批量翻译（LLM 并发/批次生效）
                pending.append({"key": key, "text": text, "sink": sink})
                if len(pending) >= batch_size:
                    await _flush()
            await _flush()

        by_mod: dict[str, dict[str, str]] = {}                          # 语言文件产物
        json_lines_translations: dict[Path, list[tuple[TextSource, dict[str, str]]]] = {}
        hard_mappings: dict[Path, dict[str, str]] = {}                  # 硬编码产物 {jar: {text: translated}}

        # 阶段 1：语言文件 jobs（批量收集 → 一次 translate_batch → 逐条写回，共用引擎/记忆/状态机）
        batch_size = getattr(engine, "batch_size", 20)
        lang_items = ({"key": job.key, "text": job.source_text,
                       "sink": by_mod.setdefault(job.modid, {})} for job in jobs)
        await _translate_batch_pipeline(
            lang_items, lambda texts: engine.translate_batch(texts, req.target_lang), batch_size)

        # 阶段 2：json/lines 全文本覆盖（批量收集 → 一次 translate_batch → 逐条写回）
        for jar, srcs in text_sources_by_jar.items():
            updates: list[tuple[TextSource, dict[str, str]]] = []
            for src in srcs:
                out: dict[str, str] = {}
                json_items = ({"key": key, "text": text, "sink": out}
                              for key, text in src.entries.items())
                await _translate_batch_pipeline(
                    json_items, lambda texts: engine.translate_batch(texts, req.target_lang), batch_size)
                if out:
                    updates.append((src, out))
            if updates:
                json_lines_translations[jar] = updates

        # 阶段 2.5：整合包目录文本源（任务线/config/data/kubejs）→ 补丁包译文
        pack_translations: list[tuple[TextSource, dict[str, str]]] = []
        if kind == "modpack" and pack_sources:
            for src in pack_sources:
                out: dict[str, str] = {}
                pack_items = ({"key": key, "text": text, "sink": out}
                              for key, text in src.entries.items())
                await _translate_batch_pipeline(
                    pack_items, lambda texts: engine.translate_batch(texts, req.target_lang), batch_size)
                if out:
                    pack_translations.append((src, out))

        # 阶段 3：硬编码（引擎分流）
        if isinstance(engine, LLMClient):
            # LLM 引擎：AI 判断「是否用户可见」并翻译（批量）
            for jar, cands in hard_candidates_by_jar.items():
                if state.cancelled:
                    # B 审查 🟡3：LLM 分支同样响应取消（照阶段 1/2 模式）
                    state.status = "cancelled"
                    store.save(state)
                    return
                await _wait_if_paused()
                try:
                    mapping = await ai_judge_translate(engine, cands, req.target_lang)
                except Exception as exc:
                    # 失败 → 仅计 failed 并跳过本批（不再累加 done，
                    # 避免异常路径 done+failed 双计超 total，B 审查 🟡4）
                    state.failed += len(cands)
                    state.progress.append({"status": "warn",
                                           "error": f"{jar.name} AI 判断硬编码失败：{exc}"})
                    continue
                if mapping:
                    hard_mappings[jar] = mapping
                    # B 审查 🔵5：AI 判断译文写回记忆，后续语言文件/其他 jar 同串直接命中
                    for text, trans in mapping.items():
                        memory.set(text, req.target_lang, trans)
                skipped = len(cands) - len(mapping)
                if skipped > 0:
                    state.progress.append({"status": "warn",
                                           "error": (f"{jar.name}: AI 判定 {skipped} 条硬编码"
                                                     f"非用户可见文本，已跳过")})
                # AI 判断已批量处理这批候选，进度按候选数推进
                state.done += len(cands)
                # B 审查 🔵6：LLM 分支补一条汇总 progress（judged/visible 便于前端展示）
                state.progress.append({"jar": jar.name, "judged": len(cands),
                                       "visible": len(mapping), "status": "done"})
                if state.done % 10 == 0:
                    memory.save()
                    store.save(state)
        elif not engine_machine:
            # 兜底引擎（测试假引擎等）：硬编码批量全翻（无 AI 判断，复用批量流水线）
            for jar, cands in hard_candidates_by_jar.items():
                mapping: dict[str, str] = {}
                hard_items = ({"key": c["text"], "text": c["text"], "sink": mapping}
                              for c in cands)
                await _translate_batch_pipeline(
                    hard_items, lambda texts: engine.translate_batch(texts, req.target_lang), batch_size)
                if mapping:
                    hard_mappings[jar] = mapping

        memory.save()
        if state.failed > 0:
            state.progress.append({"status": "warn",
                                   "error": (f"{state.failed} 条翻译失败"
                                             f"（可能因 API Key 无效或网络问题），已保留原文")})

        # 产物组织 outputs/<task_id>/ 下（exe 旁 outputs，download 从这里读）
        out_dir = outputs_dir / task_id
        out_dir.mkdir(parents=True, exist_ok=True)
        pack_format = infer_pack_format(path)
        exported: list[str] = []
        hard_count = 0
        if kind == "modjar":
            # modjar → 单一汉化 jar：语言文件 + json/lines + 硬编码全写回一个 jar 副本。
            # 命名 {原jar stem}-{语言}化.jar（zh_cn→简体中文化、zh_tw→繁体中文化）。
            for jar in jars:
                jar_copy = out_dir / f"{jar.stem}-{lang_display_name(req.target_lang)}化.jar"
                # 原 jar 只读铁律：先 copy2 副本再改
                shutil.copy2(jar, jar_copy)
                # 语言文件写回：解压副本 → 写 assets/<modid>/lang/<target>.<ext>（合并已有 zh）→ 重打包
                write_lang_into_jar(jar_copy, by_mod, req.target_lang, pack_format)
                # json/lines 全文本覆盖写回 jar 副本
                for src, trans in json_lines_translations.get(jar, []):
                    write_translated(jar_copy, src, trans)
                # 硬编码替换（同一副本）
                mapping = hard_mappings.get(jar)
                if mapping:
                    result = replace_hardcoded_strings(jar_copy, mapping)
                    if result["failed_classes"]:
                        # failed_classes 累加进 state.failed + warn
                        state.failed += len(result["failed_classes"])
                        state.progress.append({"status": "warn",
                                               "error": (f"{jar.name}: {len(result['failed_classes'])} 个 class 替换失败"
                                                         f"（已跳过保留原字节）")})
                exported.append(str(jar_copy))
                hard_count += 1
        else:
            # modpack → 模组汉化资源包（语言文件）+ 汉化补丁包（任务线/config/data/kubejs + VP 硬编码方案）
            if by_mod:
                # 资源包（语言文件）：固定名「模组汉化资源包」，所有整合包共用
                pack_out = out_dir / "模组汉化资源包.zip"
                build_resource_pack(by_mod, req.target_lang, pack_format, pack_out)
                exported.append(str(pack_out))
            # 补丁包条目：目录文本源译文（按整合包相对路径组织）
            patch_entries: list[tuple[str, str | bytes]] = [
                (src.source_path, render_pack_source(src, trans, path))
                for src, trans in pack_translations
            ]
            # 硬编码：VP 方案优先（自动下载对应版本 VP jar + 映射进补丁包），
            # 下载失败/联网不可用 → 回退 hardcoded 汉化 jar（替换 mod，兜底保底）
            vp_used = False
            if hard_mappings:
                vp_pairs: dict[str, str] = {}
                for mapping in hard_mappings.values():
                    vp_pairs.update(mapping)
                loader, mc_version = infer_modpack_runtime(path / "mods")
                vp_bytes = await download_vault_patcher(loader, mc_version)
                if vp_bytes:
                    vp_used = True
                    patch_entries.append(("vault-patcher.jar", vp_bytes))
                    patch_entries.append(("vaultpatcher/modules/mc-auto-translator.json",
                                          json.dumps(build_vp_module(vp_pairs),
                                                     ensure_ascii=False, indent=2)))
                    state.progress.append({"status": "done", "key": "vault-patcher.jar",
                                           "source": "Vault Patcher",
                                           "translated": f"已自动下载（{loader} {mc_version}），装入 mods/ 生效"})
                else:
                    state.progress.append({"status": "warn",
                                           "error": "Vault Patcher 自动下载失败（联网/版本匹配不可用），回退 hardcoded 汉化 jar"})
            if patch_entries:
                patch_out = out_dir / "汉化补丁包.zip"
                _build_patch_pack(patch_entries, patch_out)
                exported.append(str(patch_out))
            # 硬编码兜底：VP 未启用时产 hardcoded 汉化 jar（json/lines 写回 + 硬编码替换）
            do_hardcode = bool(hard_mappings) and not vp_used
            hard_dir = out_dir / "hardcoded"
            hard_used: set[str] = set()
            seq = 0
            for jar in jars:
                json_updates = json_lines_translations.get(jar)
                mapping = hard_mappings.get(jar) if do_hardcode else None
                if not json_updates and not mapping:
                    continue
                # 原 jar 只读铁律：先 copy2 到 out_dir/hardcoded/<name> 副本再改
                name = f"{task_id}_{jar.stem}.jar"
                if name in hard_used:
                    # 同名 jar（不同子目录）防覆盖：独立 seq 递增 + while 循环，
                    # 彻底避免序号名与既有名（如 stem=2_mod 的 jar）相撞（A5-review）
                    while name in hard_used:
                        seq += 1
                        name = f"{task_id}_{seq}_{jar.stem}.jar"
                hard_used.add(name)
                jar_copy = hard_dir / name
                jar_copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(jar, jar_copy)
                # json/lines 文本覆盖写回 jar 副本
                for src, trans in json_updates or []:
                    write_translated(jar_copy, src, trans)
                # 硬编码替换（同一副本）
                if mapping:
                    result = replace_hardcoded_strings(jar_copy, mapping)
                    if result["failed_classes"]:
                        # failed_classes 累加进 state.failed + warn
                        state.failed += len(result["failed_classes"])
                        state.progress.append({"status": "warn",
                                               "error": (f"{jar.name}: {len(result['failed_classes'])} 个 class 替换失败"
                                                         f"（已跳过保留原字节）")})
                exported.append(str(jar_copy))
                hard_count += 1

        if not exported:
            # 全部词条已汉化 / 全部翻译失败：done + warn，不导出空包
            state.status = "done"
            state.progress.append({"status": "warn",
                                   "error": "无可导出的翻译产物（词条均为已汉化或全部失败）"})
            store.save(state)
            return

        state.status = "done"
        # modjar 无资源包 zip：pack 字段仅 modpack 且语言文件非空时指向资源包，否则 None
        state.progress.append({"status": "done", "file": str(out_dir),
                               "pack": (str(out_dir / "模组汉化资源包.zip")
                                        if kind == "modpack" and by_mod else None),
                               "hardcoded": hard_count})
        store.save(state)
    except asyncio.CancelledError:
        # CancelledError 继承 BaseException，逃过 except Exception 会状态卡死（F2）
        state.status = "cancelled"
        store.save(state)
        raise
    except Exception as e:
        state.status = "failed"
        state.progress.append({"status": "error", "error": str(e)})
        store.save(state)
    finally:
        # 任务终态（done/failed/cancelled）后清理任务级中间产物（temp），产物保留（C）
        if state.status in ("done", "failed", "cancelled"):
            cleanup_task_work(work_dir, task_id)
