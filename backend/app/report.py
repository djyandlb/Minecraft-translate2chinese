# -*- coding: utf-8 -*-
"""翻译报告（通用：整合包 / mod / 地图 / 光影 所有模式任务结束时生成）。

大整合包失败几百条前端只显示 60 条放不完——报告把全部失败条目 + 统计 + 产物
落盘 report.json，前端任务完成后点「阅读翻译报告」弹窗阅读（不下载）。

报告数据为结构化 dict（overview/stages/products/failures/notes），前端直接渲染。
"""
import json
import time
from pathlib import Path


def build_report(input_name: str, target_lang: str, total: int, done: int, failed: int,
                 stages: list[dict], products: list[dict], failures: list[dict],
                 created_ts: float | None = None, skipped: int = 0) -> dict:
    """构造翻译报告数据（通用所有模式）。

    total/done/failed：任务统计；stages：分阶段 [{name,total,done}]；
    products：生成产物 [{name,desc,size_mb}]；failures：全部未翻译 [{text,reason,where?}]；
    skipped：不算 failed 但**跳过翻译**的量（技术串 skip + 硬编码 AI 判定非用户可见）。
    """
    done_n = int(done or 0)
    failed_n = int(failed or 0)
    total_n = max(int(total or 0), done_n)
    skipped_n = max(int(skipped or 0), 0)
    # 修复（recheck，用户诉求）：覆盖率 = 成功 / **可翻译量**（总文本 - 不算 failed 但跳过
    # 翻译的量）。跳过翻译（技术串/硬编码 AI 判定非用户可见）本来就不该翻，算进分母会虚低
    # 覆盖率（用户例：total=10000 含 2000 硬编码跳过 → 应算 8000 可翻译量）。done 含跳过
    #（skip/排除都推进 done）→ 成功 = done - 跳过；失败（该翻没翻）算进分母、不算成功。
    denom = max(total_n - skipped_n, 1)
    translated = max(0, done_n - skipped_n)
    coverage = round(translated / denom * 100, 2)
    return {
        "input": input_name or "",
        "target_lang": target_lang or "",
        "created": created_ts if created_ts is not None else time.time(),
        "overview": {
            "total": total_n,
            "translated": translated,
            "failed": failed_n,
            "coverage": coverage,
        },
        "stages": stages or [],
        "products": products or [],
        "failures": failures or [],
        "notes": [
            "漏翻未译出：AI 反复重翻仍返回原文，通常为生僻词或语境缺失，可手动补充。",
            "保留原文：专有名词 / 命令 / 代码标识 / 配置开关，保留是正确决策，不算缺陷。",
            "覆盖率 = 已翻译词条 / 总词条（未翻译计入失败）。",
        ],
    }


def save_report(report: dict, outputs_dir: Path, task_id: str) -> Path:
    """报告落盘 outputs/<task_id>/report.json（产物区，任务清理不删，可随时查看）。"""
    p = outputs_dir / task_id / "report.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return p
