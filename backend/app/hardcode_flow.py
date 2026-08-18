# -*- coding: utf-8 -*-
"""M5-2 硬编码翻译后台流程。

把硬编码汉化全流程串起来：复制原 jar 到副本 → 扫描硬编码字符串 → 引擎翻译
（记忆 + 简繁 + 术语）→ 替换 + 校验 → 输出新 jar。
复用 M2 引擎（create_engine + MemoryStore + glossary + is_same_script）。
原始 jar 只读，一切写操作只在 work 副本（复制/扫描/替换均作用于副本）。
"""
import asyncio
import shutil
from pathlib import Path

from app.cleanup import cleanup_task_work
from app.config import AppConfig
from app.safeerr import sanitize_error
from app.glossary import load_glossary, term_inject_prompt
from app.hardcode import replace_hardcoded_strings, scan_hardcoded_strings
from app.memory import MemoryStore
from app.models import HardcodeRequest
from app.tasks import TaskStore
from app.translate.engine import create_engine
from app.translate.han import is_same_script, simplify, traditional
from app.translate.llm import LLMClient


async def run_hardcode_translation(task_id: str, req: HardcodeRequest, cfg: AppConfig,
                                   store: TaskStore, work_dir: Path, outputs_dir: Path) -> None:
    """硬编码汉化后台流程：复制原 jar 到副本 → 扫描 → 引擎翻译 → 替换校验 → 输出新 jar。原 jar 只读。

    work_dir 为中间产物区（temp，副本落 jars/<task_id>，任务终态后清理）；
    outputs_dir 为产物区（exe 旁 outputs/，汉化 jar 落这里）。
    """
    state = store.load(task_id)
    memory = MemoryStore(work_dir / "memory.json")
    glossary_prompt = term_inject_prompt(load_glossary(work_dir / "glossary.json"))
    try:
        # 副本策略：原 jar 只读，一切写操作在 work 副本（按任务隔离目录，防并发互踩）
        jar_copy = work_dir / "jars" / task_id / "mod.jar"
        jar_copy.parent.mkdir(parents=True, exist_ok=True)
        # 修复：复制/扫描几百 MB jar 是同步大 IO → to_thread 防阻塞事件循环（对齐 maps/flow.py）
        await asyncio.to_thread(shutil.copy2, Path(req.path), jar_copy)
        texts = await asyncio.to_thread(scan_hardcoded_strings, jar_copy)
        state.total = len(texts)
        if not texts:
            # M5-2：jar 内无可汉化硬编码字符串 → 直接失败，不导出无意义的空包（对齐 maps_flow）
            state.status = "failed"
            state.progress.append({"status": "error", "error": "jar 内未发现可汉化的硬编码字符串"})
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
        mapping: dict[str, str] = {}

        # 修复（recheck）：逐条 translate_batch = 每条一次 HTTP 请求，硬编码候选上千时请求数
        # 爆炸。改为分桶：记忆命中 / 简繁直转直接填，需引擎翻译的攒批一次调用。
        need_engine: list[int] = []
        translated_by_idx: dict[int, str] = {}
        for i, t in enumerate(texts):
            if state.cancelled:
                state.status = "cancelled"
                store.save(state)
                return
            cached = memory.get(t, req.target_lang)
            if cached:
                translated_by_idx[i] = cached
            elif same_script:
                # 简繁双向直转，免 AI：zh_tw 走繁化，zh_cn 走简化（F5，对齐 translator.py）
                translated_by_idx[i] = traditional(t) if req.target_lang == "zh_tw" else simplify(t)
            else:
                need_engine.append(i)
        # v1.4.1：多批并发——原逐批串行（每批等 API 响应再下一批），500条/batch_size=20
        # → 25 批串行要几十秒。改为 asyncio.gather 同时发多批，受引擎全局并发池控制
        #（engine._conc_sem），不会打爆 API。
        # v1.4.2（用户「硬编码 2 小时才 12000 条，没有并行感」）：硬编码每条文本很短
        #（几十字符），API 响应比翻译快——临时提高并发（翻译并发 ×2，封顶 32），
        # 硬编码阶段独占并发槽（翻译已完成），完成后恢复原并发。
        _orig_conc = getattr(engine, "concurrency", 5)
        _hc_conc = min(32, _orig_conc * 2)
        if hasattr(engine, "set_throughput"):
            engine.set_throughput(concurrency=_hc_conc)
        _bs = getattr(engine, "batch_size", 20)
        _batches = []
        for start in range(0, len(need_engine), _bs):
            idxs = need_engine[start:start + _bs]
            _batches.append((idxs, [texts[i] for i in idxs]))

        async def _run_batch(idxs, batch_texts):
            if state.cancelled:
                return
            while state.paused and not state.cancelled:
                await asyncio.sleep(0.5)
            try:
                results = await engine.translate_batch(batch_texts, req.target_lang)
            except Exception as exc:
                results = list(batch_texts)
                for t0 in batch_texts:
                    state.failed += 1
                    state.progress.append({"status": "warn", "key": "hardcode",
                                           "error": f"翻译失败：{t0[:40]}（{type(exc).__name__}）"})
            for k, i in enumerate(idxs):
                translated_by_idx[i] = results[k] if k < len(results) else texts[i]

        await asyncio.gather(*(_run_batch(idxs, bt) for idxs, bt in _batches))
        # 恢复原并发（硬编码阶段临时提高了并发）
        if hasattr(engine, "set_throughput"):
            engine.set_throughput(concurrency=_orig_conc)
        # 写回映射 + 记忆（仅真译文写记忆：失败回原文 / AI 保留原文都不写，防记忆污染固化失败）
        for i, t in enumerate(texts):
            translated = translated_by_idx.get(i, t)
            if translated != t:
                memory.set(t, req.target_lang, translated)
            mapping[t] = translated
            state.done += 1
            if state.done % 10 == 0:
                memory.save()
                store.save(state)

        memory.save()
        # 部分翻译失败仍算完成（status=done），但进度里给用户可见警告
        if state.failed > 0:
            state.progress.append({"status": "warn",
                                   "error": f"{state.failed} 条翻译失败（具体原因见流程结束后翻译报告），已保留原文"})
        # 替换 + 校验：原地改副本并重打包（replace_hardcoded_strings 内部逐 class 校验，失败记 failed_classes）
        # 修复：解压+逐 class 重写+重打包是同步大 IO → to_thread
        result = await asyncio.to_thread(replace_hardcoded_strings, jar_copy, mapping)
        state.failed += len(result["failed_classes"])
        if result["failed_classes"]:
            state.progress.append({"status": "warn",
                                   "error": f"{len(result['failed_classes'])} 个 class 替换失败（已跳过保留原字节）"})
        # 输出：改完的副本移到 outputs（exe 旁产物区）；修复：大文件移动 to_thread
        out = outputs_dir / f"{task_id}_hardcoded.jar"
        out.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.move, str(jar_copy), str(out))
        state.progress.append({"status": "done", "file": str(out), "replaced": result["replaced"]})
        state.status = "done"
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
