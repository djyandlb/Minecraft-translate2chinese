# -*- coding: utf-8 -*-
"""A5 统一全自动翻译流程。

一个入口跑完全部：拖入整合包 / mod jar / 地图 → 自动识别 → 语言文件 + 硬编码一起翻译
（共用引擎/记忆/状态机）→ 产物 = 资源包 zip + 汉化 jar（hardcoded/ 副本）。
map 委托 maps_flow。原 jar/存档只读，一切写操作只在 work 副本。
"""
import asyncio
import shutil
from pathlib import Path

from app.archive import extract_modpack, is_archive
from app.config import AppConfig
from app.detect import (_HARDCODE_MAX_BYTES, detect_input_type, detect_source_lang,
                        infer_pack_format, needs_translation)
from app.diff import build_jobs
from app.glossary import load_glossary, term_inject_prompt
from app.hardcode import replace_hardcoded_strings, scan_hardcoded_strings
from app.maps.flow import run_map_translation
from app.memory import MemoryStore
from app.models import AutoRequest, MapTranslateRequest
from app.resourcepack import build_resource_pack
from app.scanner import scan_jar, scan_modpack
from app.tasks import TaskStore
from app.translate.engine import create_engine
from app.translate.han import is_same_script, simplify, traditional
from app.translate.llm import LLMClient


async def run_auto_translation(task_id: str, req: AutoRequest, cfg: AppConfig,
                               store: TaskStore, work_dir: Path) -> None:
    """统一全自动翻译：自动识别类型 → modpack/modjar 语言文件+硬编码并入 → 资源包+汉化 jar；map 委托。

    流程骨架照 translator.py / hardcode_flow.py：扫描 → 查记忆 → 引擎翻译
    （术语注入+简繁捷径+token统计）→ 资源包 + 硬编码副本改写。
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
                cfg, store, work_dir)
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

        # 源语言自动检测（req.source_lang 为空时）；全汉化返回 None → 兜底 en_us + warn
        auto_lang = detect_source_lang(jars, req.target_lang) if not req.source_lang else None
        if auto_lang is None and not req.source_lang:
            # 可继续：缺口为 0 自然空包，由下游空词条/无可导出分支覆盖
            state.progress.append({"status": "warn",
                                   "error": "源语言自动检测：所有资源已是目标语言，无空缺可翻（以 en_us 兜底继续）"})
        source_lang = req.source_lang or auto_lang or "en_us"

        # ① 语言文件扫描 → jobs
        scans = (scan_modpack(path, source_lang, req.target_lang, "mods") if kind == "modpack"
                 else scan_jar(path, source_lang, req.target_lang))
        jobs = build_jobs(scans)

        # ② 硬编码扫描（逐 jar 异常兜底，超大 jar 复用 A1 _HARDCODE_MAX_BYTES 上限跳过深扫）
        hard_items: list[tuple[Path, str]] = []   # (jar, text)
        for jar in jars:
            try:
                if jar.stat().st_size > _HARDCODE_MAX_BYTES:
                    state.progress.append({"status": "warn",
                                           "error": (f"跳过超大 jar {jar.name} 的硬编码扫描"
                                                     f"（>{_HARDCODE_MAX_BYTES // 1024 // 1024}MB）")})
                    continue
                for s in scan_hardcoded_strings(jar):
                    hard_items.append((jar, s))
            except Exception as e:
                # 损坏 jar 异常兜底跳过，不让一个坏文件中断整包流程
                state.progress.append({"status": "warn",
                                       "error": f"扫描 {jar.name} 硬编码字符串失败：{e}"})

        state.total = len(jobs) + len(hard_items)
        if not jobs and not hard_items:
            # 空词条：直接 done + warn，不导出空包
            state.status = "done"
            state.progress.append({"status": "warn",
                                   "error": "未发现可翻译的词条（语言文件缺口与硬编码字符串都为空）"})
            store.save(state)
            return
        store.save(state)

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

        same_script = is_same_script(source_lang, req.target_lang)

        async def translate_one(text: str) -> tuple[str, bool]:
            """统一翻译核心：记忆 → 简繁直转 → 引擎。返回 (译文, 是否走引擎)。"""
            cached = memory.get(text, req.target_lang)
            if cached:
                return cached, False
            if same_script:
                # 简繁双向直转，免 AI：zh_tw 走繁化，zh_cn 走简化（F5）
                return (traditional(text) if req.target_lang == "zh_tw" else simplify(text)), False
            return (await engine.translate_batch([text], req.target_lang))[0], True

        by_mod: dict[str, dict[str, str]] = {}          # 语言文件产物
        by_jar_map: dict[Path, dict[str, str]] = {}     # 硬编码产物 {jar: {text: translated}}

        # 阶段 1：语言文件 job（统一循环，共用引擎/记忆/状态机）
        for job in jobs:
            if state.cancelled:
                state.status = "cancelled"
                store.save(state)
                return
            while state.paused and not state.cancelled:
                # Y4：暂停等待也必须响应取消，否则取消被暂停卡死
                await asyncio.sleep(0.5)
            if not same_script and not needs_translation(job.source_text, req.target_lang):
                # 已汉化（含 CJK）/ 技术串：跳过翻译，计 done，不入产物。
                # 注意：same_script（简繁互转）时中文源文本必须保留翻译，跳过会漏转繁体。
                state.done += 1
                state.progress.append({"key": job.key, "source": job.source_text,
                                       "translated": job.source_text, "status": "done"})
                continue
            translated, from_engine = await translate_one(job.source_text)
            if from_engine and translated == job.source_text:
                # Y3：引擎失败回原文 → 计入 failed，前端醒目提示
                state.failed += 1
            memory.set(job.source_text, req.target_lang, translated)
            by_mod.setdefault(job.modid, {})[job.key] = translated
            state.done += 1
            state.progress.append({"key": job.key, "source": job.source_text,
                                   "translated": translated, "status": "done"})
            if state.done % 10 == 0:
                memory.save()
                store.save(state)

        # 阶段 2：硬编码 text（同一套循环逻辑）
        for jar, text in hard_items:
            if state.cancelled:
                state.status = "cancelled"
                store.save(state)
                return
            while state.paused and not state.cancelled:
                await asyncio.sleep(0.5)
            if not same_script and not needs_translation(text, req.target_lang):
                state.done += 1
                state.progress.append({"key": text, "source": text,
                                       "translated": text, "status": "done"})
                continue
            translated, from_engine = await translate_one(text)
            if from_engine and translated == text:
                state.failed += 1
            memory.set(text, req.target_lang, translated)
            by_jar_map.setdefault(jar, {})[text] = translated
            state.done += 1
            state.progress.append({"key": text, "source": text,
                                   "translated": translated, "status": "done"})
            if state.done % 10 == 0:
                memory.save()
                store.save(state)

        memory.save()
        if state.failed > 0:
            state.progress.append({"status": "warn",
                                   "error": (f"{state.failed} 条翻译失败"
                                             f"（可能因 API Key 无效或网络问题），已保留原文")})

        # 产物组织 work/outputs/<task_id>/ 下
        out_dir = work_dir / "outputs" / task_id
        out_dir.mkdir(parents=True, exist_ok=True)
        pack_format = infer_pack_format(path)
        exported: list[str] = []
        if by_mod:
            # 资源包（语言文件）
            pack_out = out_dir / f"{task_id}_{req.target_lang}.zip"
            build_resource_pack(by_mod, req.target_lang, pack_format, pack_out)
            exported.append(str(pack_out))
        hard_dir = out_dir / "hardcoded"
        hard_used: set[str] = set()
        hard_count = 0
        seq = 0
        for jar, mapping in by_jar_map.items():
            if not mapping:
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
        state.progress.append({"status": "done", "file": str(out_dir),
                               "pack": str(out_dir / f"{task_id}_{req.target_lang}.zip") if by_mod else None,
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
