"""M4-6 地图翻译后台流程。

把地图汉化全流程串起来：整档复制→副本扫描→引擎翻译（记忆+简繁+术语）→写回→导出 mcworld。
复用 M2 引擎（create_engine + MemoryStore + glossary + is_same_script）。原档只读，全部操作在副本。
"""
import asyncio
from pathlib import Path

from app.cleanup import cleanup_task_work
from app.config import AppConfig
from app.glossary import load_glossary, term_inject_prompt
from app.maps.copy import copy_world
from app.maps.export import export_world
from app.maps.scan import scan_world
from app.maps.write import write_translations
from app.memory import MemoryStore
from app.models import MapTranslateRequest
from app.tasks import TaskStore
from app.translate.engine import create_engine
from app.translate.han import is_same_script, simplify, traditional
from app.translate.llm import LLMClient


async def run_map_translation(task_id: str, req: MapTranslateRequest, cfg: AppConfig,
                              store: TaskStore, work_dir: Path, outputs_dir: Path) -> None:
    """地图翻译：整档复制→副本扫描→引擎翻译（记忆+简繁+术语）→写回→导出 mcworld。原档不动。
    局限：.mca 区块（命令方块文本）写回暂不支持，翻译前按后缀过滤，避免白烧 token。

    work_dir 为中间产物区（temp，副本落 maps/<task_id>，任务终态后清理）；
    outputs_dir 为产物区（exe 旁 outputs/，mcworld 落这里）。
    """
    state = store.load(task_id)
    memory = MemoryStore(work_dir / "memory.json")
    glossary_prompt = term_inject_prompt(load_glossary(work_dir / "glossary.json"))
    try:
        copy = work_dir / "maps" / task_id
        copy_world(Path(req.path), copy)
        entries = scan_world(copy)
        # M4-6：scan_world 会扫出 .mca 区块里的命令方块文本，但 write_translations 只支持
        # .dat/.json/.mcfunction 写回。若把 .mca 词条喂给引擎会白烧 token，此处翻译前按后缀过滤，
        # 只保留写回模块能落盘的后缀（.mca 写回暂不支持，扫描会漏命令方块）。
        total_scanned = len(entries)
        write_supported = {".dat", ".json", ".mcfunction"}
        entries = [e for e in entries if Path(e["file"]).suffix.lower() in write_supported]
        skipped = total_scanned - len(entries)
        state.total = len(entries)
        if skipped:
            state.progress.append({"status": "warn",
                                   "error": f"跳过 {skipped} 条 .mca 区块词条（写回暂不支持，命令方块文本暂无法汉化）"})
        # M4-recheck：过滤后无可写回词条（如纯命令方块世界）→ 直接失败，不导出空包
        if not entries:
            state.status = "failed"
            state.progress.append({"status": "error",
                                   "error": "世界无可写回的可翻译文本（命令方块等 .mca 区块文本暂不支持写回）"})
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

        same_script = is_same_script(req.source_lang, req.target_lang)
        by_file: dict[str, list[dict]] = {}

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
            elif same_script:
                # 简繁双向直转，免 AI：zh_tw 走繁化，zh_cn 走简化（F5，对齐 translator.py）
                translated = traditional(e["text"]) if req.target_lang == "zh_tw" else simplify(e["text"])
            else:
                translated = (await engine.translate_batch([e["text"]], req.target_lang))[0]
                # M4-recheck：引擎返回原文视为翻译失败（API Key 无效/网络问题），静默写回原文会误导用户
                if translated == e["text"]:
                    state.failed += 1
            memory.set(e["text"], req.target_lang, translated)
            by_file.setdefault(e["file"], []).append({**e, "translated": translated})
            state.done += 1
            if state.done % 10 == 0:
                memory.save()
                store.save(state)

        # M4-recheck：部分翻译失败仍算完成（status=done），但进度里给用户可见警告
        if state.failed > 0:
            state.progress.append({"status": "warn",
                                   "error": f"{state.failed} 条翻译失败（可能因 API Key 无效或网络问题），已保留原文"})
        memory.save()
        for f, trs in by_file.items():
            write_translations(Path(f), trs)
        out = outputs_dir / f"{task_id}_{req.target_lang}.mcworld"
        export_world(copy, out)
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
        state.progress.append({"status": "error", "error": str(e)})
        store.save(state)
    finally:
        # 任务终态（done/failed/cancelled）后清理任务级中间产物（temp），产物保留（C）
        if state.status in ("done", "failed", "cancelled"):
            cleanup_task_work(work_dir, task_id)
