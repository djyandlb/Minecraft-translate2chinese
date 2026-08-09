"""M4-6 地图翻译后台流程。

把地图汉化全流程串起来：整档复制→副本扫描→引擎翻译（记忆+简繁+术语）→写回→导出 mcworld。
复用 M2 引擎（create_engine + MemoryStore + glossary + is_same_script）。原档只读，全部操作在副本。
"""
import asyncio
from pathlib import Path

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
from app.translate.han import is_same_script, traditional
from app.translate.llm import LLMClient


async def run_map_translation(task_id: str, req: MapTranslateRequest, cfg: AppConfig,
                              store: TaskStore, work_dir: Path) -> None:
    """地图翻译：整档复制→副本扫描→引擎翻译（记忆+简繁+术语）→写回→导出 mcworld。原档不动。"""
    state = store.load(task_id)
    memory = MemoryStore(work_dir / "memory.json")
    glossary_prompt = term_inject_prompt(load_glossary(work_dir / "glossary.json"))
    try:
        copy = work_dir / "maps" / task_id
        copy_world(Path(req.path), copy)
        entries = scan_world(copy)
        state.total = len(entries)
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
            elif same_script and req.target_lang == "zh_tw":
                translated = traditional(e["text"])
            else:
                translated = (await engine.translate_batch([e["text"]], req.target_lang))[0]
            memory.set(e["text"], req.target_lang, translated)
            by_file.setdefault(e["file"], []).append({**e, "translated": translated})
            state.done += 1
            if state.done % 10 == 0:
                memory.save()
                store.save(state)

        memory.save()
        for f, trs in by_file.items():
            write_translations(Path(f), trs)
        out = work_dir / "outputs" / f"{task_id}_{req.target_lang}.mcworld"
        export_world(copy, out)
        state.status = "done"
        state.progress.append({"status": "done", "file": str(out)})
        store.save(state)
    except Exception as e:
        state.status = "failed"
        state.progress.append({"status": "error", "error": str(e)})
        store.save(state)
