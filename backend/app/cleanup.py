# -*- coding: utf-8 -*-
"""任务级中间产物清理（任务 C）。

任务终态（done/failed/cancelled）后调用，删除 WORK_DIR 下该任务的临时子目录
（jars/extracted/maps/uploads 的 <task_id>），保留全局 memory.json/glossary.json/tasks/。
产物 OUTPUTS_DIR 不受影响（download 从那里读）。

独立模块避免 main ↔ flow 循环导入（flow 收尾清理，main import flow）。
"""
import shutil
from pathlib import Path

# 各 flow 会创建的任务级临时子目录（中间产物，任务终态后无保留价值）
_TASK_SUBDIRS = ("jars", "extracted", "maps", "uploads")


def cleanup_task_work(work_dir: Path, task_id: str) -> None:
    """删除任务级临时目录；目录不存在/已删/删失败均幂等跳过（ignore_errors）。"""
    for sub in _TASK_SUBDIRS:
        d = work_dir / sub / task_id
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
