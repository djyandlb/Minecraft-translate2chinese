# -*- coding: utf-8 -*-
"""任务级中间产物清理（任务 C）。

任务终态（done/failed/cancelled）后调用，删除 WORK_DIR 下该任务的临时子目录
（jars/extracted/maps/uploads 的 <task_id>），保留全局 memory.json/glossary.json/tasks/。
产物 OUTPUTS_DIR 不受影响（download 从那里读）。

独立模块避免 main ↔ flow 循环导入（flow 收尾清理，main import flow）。
"""
import shutil
import time
from pathlib import Path

# 各 flow 会创建的任务级临时子目录（中间产物，任务终态后无保留价值）
# build：整合包散装组装区（resourcepacks/mods/补丁等在此组织，打完 zip 后整体清理——
# 产物文件夹只留打包好的成品，不留散装，用户实测打开产物文件夹一地散装）
_TASK_SUBDIRS = ("jars", "extracted", "maps", "uploads", "build")

# uploads 遗留目录 TTL：upload 落盘到 work/uploads/<随机 uuid>/（非 task_id），任务收尾
# 按 task_id 删不到 → 大整合包上传文件永久残留。按 mtime 清理超过 7 天的遗留目录
#（上传后立即开始任务；跨周未开始的视为可清，断点续联 path 指向 extracted 不受影响）
_UPLOAD_TTL_DAYS = 7


def cleanup_task_work(work_dir: Path, task_id: str) -> None:
    """删除任务级临时目录；目录不存在/已删/删失败均幂等跳过（ignore_errors）。

    修复（recheck）：task_id 加格式白名单（防御纵深）——所有调用方均来自 _spawn_task 的
    uuid 前缀，但本函数直接拼路径 + rmtree，误传含 ../ 的 task_id 会越界删目录。
    """
    import re as _re
    if not _re.fullmatch(r"[0-9a-fA-F-]{1,64}", task_id or ""):
        return
    for sub in _TASK_SUBDIRS:
        d = work_dir / sub / task_id
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
    # 修复（recheck）：uploads 目录名是随机 uuid（/api/upload 生成，与 task_id 无关），
    # 上面的按 task_id 删除永远清不到 → 大整合包上传文件任务结束后永久残留、work 目录
    # 持续膨胀。按 mtime 清理过期遗留目录（近期 upload 不误删）。
    _up = work_dir / "uploads"
    if _up.is_dir():
        _now = time.time()
        for d in _up.iterdir():
            if not d.is_dir():
                continue
            try:
                if _now - d.stat().st_mtime > _UPLOAD_TTL_DAYS * 86400:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass
