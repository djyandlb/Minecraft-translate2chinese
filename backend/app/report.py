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
                 created_ts: float | None = None) -> dict:
    """构造翻译报告数据（通用所有模式）。

    total/done/failed：任务统计；stages：分阶段 [{name,total,done}]；
    products：生成产物 [{name,desc,size_mb}]；failures：全部未翻译 [{text,reason,where?}]。
    """
    done_n = int(done or 0)
    failed_n = int(failed or 0)
    total_n = max(int(total or 0), done_n)
    # 修复（recheck）：之前 total 被撑到 done+failed、translated=total-failed → 失败条目也计入
    #「已翻译」，覆盖率虚高（全部处理完但 failed>0 时 coverage 仍接近 100%）。改为：
    # translated = 真实成功数 = done - failed（done 是处理计数，含失败条目），覆盖率为成功/总预估。
    translated = max(0, done_n - failed_n)
    coverage = round(translated / max(total_n, 1) * 100, 2)
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
