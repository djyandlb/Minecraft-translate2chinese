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

from app.config import AppConfig
from app.glossary import load_glossary, term_inject_prompt
from app.hardcode import replace_hardcoded_strings, scan_hardcoded_strings
from app.memory import MemoryStore
from app.models import HardcodeRequest
from app.tasks import TaskStore
from app.translate.engine import create_engine
from app.translate.han import is_same_script, simplify, traditional
from app.translate.llm import LLMClient


async def run_hardcode_translation(task_id: str, req: HardcodeRequest, cfg: AppConfig,
                                   store: TaskStore, work_dir: Path) -> None:
    """硬编码汉化后台流程：复制原 jar 到副本 → 扫描 → 引擎翻译 → 替换校验 → 输出新 jar。原 jar 只读。"""
    state = store.load(task_id)
    memory = MemoryStore(work_dir / "memory.json")
    glossary_prompt = term_inject_prompt(load_glossary(work_dir / "glossary.json"))
    try:
        # 副本策略：原 jar 只读，一切写操作在 work 副本（按任务隔离目录，防并发互踩）
        jar_copy = work_dir / "jars" / task_id / "mod.jar"
        jar_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(req.path), jar_copy)
        texts = scan_hardcoded_strings(jar_copy)
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

        for t in texts:
            if state.cancelled:
                state.status = "cancelled"
                store.save(state)
                return
            while state.paused and not state.cancelled:
                await asyncio.sleep(0.5)
            cached = memory.get(t, req.target_lang)
            if cached:
                translated = cached
            elif same_script:
                # 简繁双向直转，免 AI：zh_tw 走繁化，zh_cn 走简化（F5，对齐 translator.py）
                translated = traditional(t) if req.target_lang == "zh_tw" else simplify(t)
            else:
                translated = (await engine.translate_batch([t], req.target_lang))[0]
                # M5-2：引擎返回原文视为翻译失败（API Key 无效/网络问题），静默写回原文会误导用户
                if translated == t:
                    state.failed += 1
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
                                   "error": f"{state.failed} 条翻译失败（可能因 API Key 无效或网络问题），已保留原文"})
        # 替换 + 校验：原地改副本并重打包（replace_hardcoded_strings 内部逐 class 校验，失败记 failed_classes）
        result = replace_hardcoded_strings(jar_copy, mapping)
        state.failed += len(result["failed_classes"])
        if result["failed_classes"]:
            state.progress.append({"status": "warn",
                                   "error": f"{len(result['failed_classes'])} 个 class 替换失败（已跳过保留原字节）"})
        # 输出：改完的副本移到 outputs
        out = work_dir / "outputs" / f"{task_id}_hardcoded.jar"
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(jar_copy), str(out))
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
        state.progress.append({"status": "error", "error": str(e)})
        store.save(state)
