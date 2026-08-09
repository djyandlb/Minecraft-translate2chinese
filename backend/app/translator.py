# 翻译流程核心（任务 13，V3 接线）：扫描→查记忆→引擎翻译（术语注入+简繁捷径+token统计）→资源包
import asyncio
from pathlib import Path

from app.archive import is_archive, extract_modpack
from app.cleanup import cleanup_task_work
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
                          store: TaskStore, work_dir: Path, outputs_dir: Path) -> None:
    """后台翻译流程：扫描→查记忆→引擎翻译（术语注入+简繁捷径+token统计）→资源包。

    work_dir 为中间产物区（temp，任务终态后清理任务级子目录）；
    outputs_dir 为产物区（exe 旁 outputs/，资源包落这里，download 从这里读）。
    """
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
                 else scan_modpack(path, req.source_lang, req.target_lang, req.scope))
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
        if isinstance(engine, LLMClient) and not engine.api_key:
            # R1：keyring 空 key → 引擎主路径假成功，提前告警
            state.progress.append({"status": "warn", "error": "未配置 API Key，AI 翻译将失败，请在配置页填写"})

        same_script = is_same_script(req.source_lang, req.target_lang)
        # Y2：pack_format 优先级 显式 > mc_version > 默认 1.20.1
        pack_format = (req.pack_format or
                       (version_to_pack_format(req.mc_version) if req.mc_version
                        else version_to_pack_format("1.20.1")))
        by_mod: dict[str, dict[str, str]] = {}

        for job in jobs:
            if state.cancelled:
                state.status = "cancelled"
                store.save(state)
                return
            while state.paused and not state.cancelled:
                # Y4：暂停等待也必须响应取消，否则取消被暂停卡死
                await asyncio.sleep(0.5)
            cached = memory.get(job.source_text, req.target_lang)
            from_engine = False
            if cached:
                translated = cached
            elif same_script:
                # 简繁双向直转，免 AI：zh_tw 走繁化，zh_cn 走简化（F5）
                translated = traditional(job.source_text) if req.target_lang == "zh_tw" else simplify(job.source_text)
            else:
                translated = (await engine.translate_batch([job.source_text], req.target_lang))[0]
                from_engine = True
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

        memory.save()
        out = outputs_dir / f"{task_id}_{req.target_lang}.zip"
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
    finally:
        # 任务终态（done/failed/cancelled）后清理任务级中间产物（temp），产物保留（C）
        if state.status in ("done", "failed", "cancelled"):
            cleanup_task_work(work_dir, task_id)
