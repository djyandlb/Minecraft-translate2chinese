# 翻译流程核心（任务 13，V3 接线）：扫描→查记忆→引擎翻译（术语注入+简繁捷径+token统计）→资源包
import asyncio
from pathlib import Path

from app.archive import is_archive, extract_modpack
from app.config import AppConfig
from app.diff import build_jobs
from app.glossary import load_glossary, term_inject_prompt
from app.memory import MemoryStore
from app.models import TranslateRequest
from app.resourcepack import build_resource_pack
from app.scanner import scan_modpack, scan_jar
from app.tasks import TaskState, TaskStore
from app.translate.engine import create_engine
from app.translate.han import is_same_script, simplify, traditional
from app.translate.llm import LLMClient
from app.version import version_to_pack_format


async def run_translation(task_id: str, req: TranslateRequest, cfg: AppConfig,
                          store: TaskStore, work_dir: Path) -> None:
    """后台翻译流程：扫描→查记忆→引擎翻译（术语注入+简繁捷径+token统计）→资源包。"""
    state = store.load(task_id)
    memory = MemoryStore(work_dir / "memory.json")
    glossary = load_glossary(work_dir / "glossary.json")
    glossary_prompt = term_inject_prompt(glossary)
    try:
        path = Path(req.path)
        if is_archive(path):
            # 按任务隔离解压目录，避免并发任务共用 extracted/ 互相覆盖（F4）
            path = extract_modpack(path, work_dir / "extracted" / task_id)
        scans = (scan_jar(path, req.source_lang, req.target_lang)
                 if req.mode == "jar"
                 else scan_modpack(path, req.source_lang, req.target_lang))
        jobs = build_jobs(scans)
        state.total = len(jobs)
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
        pack_format = req.pack_format or version_to_pack_format("1.20.1")
        by_mod: dict[str, dict[str, str]] = {}

        for job in jobs:
            if state.cancelled:
                state.status = "cancelled"
                store.save(state)
                return
            while state.paused:
                await asyncio.sleep(0.5)
            cached = memory.get(job.source_text, req.target_lang)
            if cached:
                translated = cached
            elif same_script:
                # 简繁双向直转，免 AI：zh_tw 走繁化，zh_cn 走简化（F5）
                translated = traditional(job.source_text) if req.target_lang == "zh_tw" else simplify(job.source_text)
            else:
                translated = (await engine.translate_batch([job.source_text], req.target_lang))[0]
            memory.set(job.source_text, req.target_lang, translated)
            by_mod.setdefault(job.modid, {})[job.key] = translated
            state.done += 1
            state.progress.append({"key": job.key, "source": job.source_text,
                                   "translated": translated, "status": "done"})
            if state.done % 10 == 0:
                memory.save()
                store.save(state)

        memory.save()
        out = work_dir / "outputs" / f"{task_id}_{req.target_lang}.zip"
        build_resource_pack(by_mod, req.target_lang, pack_format, out)
        state.status = "done"
        state.progress.append({"status": "done", "file": str(out)})
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
