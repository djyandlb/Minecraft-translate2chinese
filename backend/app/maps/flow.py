"""M4-6 地图翻译后台流程。

把地图汉化全流程串起来：整档复制→副本扫描→引擎翻译（记忆+简繁+术语）→写回→导出 mcworld。
复用 M2 引擎（create_engine + MemoryStore + glossary + is_same_script）。原档只读，全部操作在副本。
"""
import asyncio
import re
import sys
import tempfile
from pathlib import Path

from app.archive import clean_extracted_cache, dir_fingerprint
from app.cleanup import cleanup_task_work
from app.config import AppConfig
from app.glossary import load_glossary, term_inject_prompt
from app.maps.copy import copy_world
from app.maps.export import export_world
from app.maps.scan import scan_world
from app.safeerr import sanitize_error
from app.maps.write import write_translations
from app.memory import MemoryStore
from app.models import MapTranslateRequest
from app.placeholder import clean_surrogates
from app.report import build_report, save_report
from app.tasks import TaskStore
from app.translate.engine import create_engine
from app.translate.han import is_same_script, simplify, traditional
from app.translate.llm import LLMClient


def _is_cjk(t: str) -> bool:
    """含 CJK 字符即视为中文源（简繁直转判断用）。"""
    return bool(re.search(r"[一-鿿]", t))


def _safe_work_dir(work_dir: Path) -> Path:
    """归一化中间产物目录：绝对 + 拒绝 PyInstaller 解压目录（_MEIPASS，onefile 只读）。

    用户实测旧 exe 把世界副本写进 Temp\\MEIxxxx\\app\\maps → Permission denied。
    work_dir 若落在 _MEIPASS 内（含其子目录），强制回退系统 temp 的 mc-translator。
    """
    p = Path(work_dir).resolve()
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        base = Path(meipass).resolve()
        if p == base or (hasattr(p, "is_relative_to") and p.is_relative_to(base)):
            return Path(tempfile.gettempdir()) / "mc-translator"
    return p


async def run_map_translation(task_id: str, req: MapTranslateRequest, cfg: AppConfig,
                              store: TaskStore, work_dir: Path, outputs_dir: Path) -> None:
    """地图翻译：整档复制→副本扫描→引擎翻译（记忆+简繁+术语）→写回→导出 mcworld。原档不动。
    .dat/.json/.mcfunction/.mca 均支持写回（.mca 为整 region 重写）。

    work_dir 为中间产物区（temp，副本落 maps/<task_id>，任务终态后清理）；
    outputs_dir 为产物区（exe 旁 outputs/，mcworld 落这里）。
    """
    state = store.load(task_id)
    # 修复（recheck）：地图任务从未设置 display_name → 任务快照右栏标题为空（auto_flow
    # 都有设置，map 漏网）。与 auto_flow 一致：取原始输入文件名（去扩展名）。
    if not state.display_name:
        state.display_name = Path(req.path).stem.strip() or "地图"
        store.save(state)
    work_dir = _safe_work_dir(work_dir)   # 防御：world 副本/记忆绝落 temp，不碰 _MEIPASS
    memory = MemoryStore(work_dir / "memory.json")
    glossary_prompt = term_inject_prompt(load_glossary(work_dir / "glossary.json"))
    failures: list[dict] = []             # 未翻译条目收集（翻译报告用，通用所有模式）
    try:
        # 阶段预设提示（用户诉求：地图模式也要有对应进程提示，扫描存档前推即时反馈）
        state.progress.append({"status": "translating", "count": 0,
                               "note": "正在扫描地图存档…"})
        store.save(state)
        # 相同存档断点重连（用户诉求）：按目录指纹缓存副本，不重复复制几 GB 存档。
        # 已汉化条目（副本已写回中文）scan 时按 CJK 跳过；未汉化的走 memory/AI。
        # 复制成功后清理旧副本（只留最近 3 个）。
        copy = work_dir / "maps" / dir_fingerprint(Path(req.path))
        if not (copy / ".done").exists():
            # 修复：整档复制几 GB 是同步大 IO，阻塞事件循环（其他请求/SSE 卡死）→ to_thread
            await asyncio.to_thread(copy_world, Path(req.path), copy)
            (copy / ".done").write_text("ok", encoding="utf-8")
            clean_extracted_cache(work_dir / "maps")
        entries = await asyncio.to_thread(scan_world, copy, req.target_lang)
        state.total = len(entries)
        # M4-recheck：无任何可翻译词条 → 直接失败，不导出空包
        if not entries:
            state.status = "failed"
            state.progress.append({"status": "error",
                                   "error": "世界无可写回的可翻译文本"})
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
            # R1：keyring 空 key → 引擎主路径假成功，提前告警（对齐 translator.py）
            state.progress.append({"status": "warn", "error": "未配置 API Key，AI 翻译将失败，请在配置页填写"})

        # 阶段预设提示 + AI 增强（同 auto_flow._smart_status：LLM 引擎后台让 AI 生成状态描述）
        async def _status(preset: str) -> None:
            state.progress.append({"status": "translating", "count": 0, "note": preset})
            store.save(state)
            if not isinstance(engine, LLMClient):
                return

            async def _gen() -> None:
                try:
                    note = await engine.generate_status(preset, "")
                except Exception:
                    return
                if state.status not in ("done", "failed", "cancelled"):
                    state.progress.append({"status": "translating", "count": 0, "note": note})
                    store.save(state)
            asyncio.create_task(_gen())

        await _status("正在翻译地图文本…")

        same_script = is_same_script(req.source_lang, req.target_lang)
        by_file: dict[str, list[dict]] = {}
        batch_size = getattr(engine, "batch_size", 20)   # 修复：攒批翻译（原来逐条一次 HTTP 往返，极慢且易限流）
        pending: list[dict] = []

        async def _flush_pending() -> None:
            """攒满一批需引擎词条 → 一次 translate_batch → 逐条回填。"""
            if not pending:
                return
            texts = [e["text"] for e in pending]
            state.progress.append({"status": "translating", "count": len(pending),
                                   "note": "地图翻译"})
            store.save(state)
            note = ""
            try:
                translated_list = await engine.translate_batch(texts, req.target_lang)
            except Exception as exc:
                # 整批引擎异常（Key 无效/网络/配置缺失）：全部计失败保留原文，不中断整任务
                note = f"（异常：{type(exc).__name__}）"
                translated_list = [e["text"] for e in pending]
            if len(translated_list) < len(pending):
                # 防引擎返回条数不足
                translated_list += [pending[len(translated_list)]["text"]
                                    for _ in range(len(pending) - len(translated_list))]
            for e, translated in zip(pending, translated_list):
                translated = clean_surrogates(translated)   # 修复：清除无效 surrogate（utf-8 写盘崩溃）
                if translated == e["text"]:
                    # 失败明细进 progress：用户能直接看到是哪几条、什么原文
                    state.failed += 1
                    state.progress.append({"status": "warn", "key": "map",
                                           "error": f"翻译失败：{e['text'][:40]}{note}"})
                    failures.append({"text": e["text"][:50],
                                     "reason": f"翻译失败{note}",
                                     "where": str(e.get("file", ""))})
                else:
                    # 修复：仅真译文写记忆（失败回原文不写，防记忆被原文污染固化失败）
                    memory.set(e["text"], req.target_lang, translated)
                # 逐条翻译明细（用户诉求：地图也要像 mod/整合包一样有翻译行）
                state.progress.append({"key": str(e.get("key") or "map"), "source": e["text"],
                                       "translated": translated, "status": "done",
                                       "file": e["file"]})
                by_file.setdefault(e["file"], []).append({**e, "translated": translated})
                state.done += 1
            if state.done % 10 == 0:
                memory.save()
                store.save(state)
            pending.clear()

        for e in entries:
            if state.cancelled:
                state.status = "cancelled"
                store.save(state)
                return
            while state.paused and not state.cancelled:
                await asyncio.sleep(0.5)
            cached = memory.get(e["text"], req.target_lang)
            if cached:
                translated = cached
            elif _is_cjk(e["text"]) and req.target_lang in ("zh_cn", "zh_tw"):
                # 修复：中文源（繁体/方言作者地图）scan 已收集 → 简繁直转（不调 AI）。
                # 已是目标形态（断点重连已翻）时 simplify/traditional 返回原文，写回不变，无害
                translated = traditional(e["text"]) if req.target_lang == "zh_tw" else simplify(e["text"])
                if translated != e["text"]:
                    memory.set(e["text"], req.target_lang, translated)
            elif same_script:
                # 简繁双向直转，免 AI：zh_tw 走繁化，zh_cn 走简化（F5，对齐 translator.py）
                translated = traditional(e["text"]) if req.target_lang == "zh_tw" else simplify(e["text"])
                memory.set(e["text"], req.target_lang, translated)
            else:
                pending.append(e)
                if len(pending) >= batch_size:
                    await _flush_pending()
                continue
            # 逐条翻译明细（记忆命中/简繁直转也显示，用户诉求）
            translated = clean_surrogates(translated)   # 修复：清除无效 surrogate（utf-8 写盘崩溃）
            state.progress.append({"key": str(e.get("key") or "map"), "source": e["text"],
                                   "translated": translated, "status": "done",
                                   "file": e["file"]})
            by_file.setdefault(e["file"], []).append({**e, "translated": translated})
            state.done += 1
            if state.done % 10 == 0:
                memory.save()
                store.save(state)
        await _flush_pending()

        # M4-recheck：部分翻译失败仍算完成（status=done），但进度里给用户可见警告
        if state.failed > 0:
            state.progress.append({"status": "warn",
                                   "error": f"{state.failed} 条翻译失败（具体原因见流程结束后翻译报告），已保留原文"})
        memory.save()
        await _status("正在写回地图存档…")
        for f, trs in by_file.items():
            try:
                # 修复：NBT 整 region 重写是同步大 IO → to_thread 防阻塞事件循环
                await asyncio.to_thread(write_translations, Path(f), trs)
            except PermissionError as e:
                # 明确定位：哪个文件 + 占用/只读提示（用户实测裸 Errno 13 无法定位）。
                # 修复（recheck）：f 是临时目录完整路径——只显示文件名，完整目录脱敏
                # （防本机路径泄露给前端）
                raise PermissionError(
                    f"写回 {Path(f).name} 失败（文件被占用或所在目录只读，"
                    f"可能是杀毒软件/上一次任务句柄未释放）：{sanitize_error(str(e))}") from e
        out = outputs_dir / f"{task_id}_{req.target_lang}.mcworld"
        # 修复：导出打包 zip 同步大 IO → to_thread
        await asyncio.to_thread(export_world, copy, out)
        state.status = "done"
        state.progress.append({"status": "done", "file": str(out)})
        store.save(state)
    except asyncio.CancelledError:
        # CancelledError 继承 BaseException，逃过 except Exception 会状态卡死（F2，对齐 translator.py）
        state.status = "cancelled"
        store.save(state)
        raise
    except Exception as e:
        state.status = "failed"
        state.progress.append({"status": "error", "error": sanitize_error(str(e))})
        store.save(state)
    finally:
        # 任务终态（done/failed/cancelled）后清理任务级中间产物（temp），产物保留（C）
        if state.status in ("done", "failed", "cancelled"):
            cleanup_task_work(work_dir, task_id)
            # 生成翻译报告（地图模式，通用所有模式）：report.json 落产物区，前端弹窗阅读
            try:
                _out = locals().get("out")
                _products = []
                if _out is not None and _out.exists():
                    _products.append({"name": _out.name,
                                      "desc": "汉化地图存档（mcworld）",
                                      "size_mb": round(_out.stat().st_size / 1048576, 1)})
                report = build_report(
                    input_name=state.display_name or Path(req.path).name,
                    target_lang=req.target_lang,
                    total=state.total, done=state.done, failed=state.failed,
                    stages=[{"name": "翻译", "total": state.total, "done": state.done}],
                    products=_products,
                    failures=failures,
                )
                save_report(report, outputs_dir, task_id)
            except Exception:
                pass
