"""M4 地图汉化 - 世界存档识别模块。

提供世界目录的合法性校验、基本信息读取与扫描目标文件列举。
全程基于 pathlib 与 nbtlib 实现。
"""
from pathlib import Path
from nbtlib import File

# 扫描用目标文件后缀
_SCAN_SUFFIXES = (".dat", ".mca", ".json", ".mcfunction")


def validate_world(world_path: Path) -> bool:
    """校验世界目录：level.dat 存在且可被 nbtlib 加载。"""
    try:
        File.load(world_path / "level.dat", gzipped=True)
        return True
    except Exception:
        return False


def get_world_info(world_path: Path) -> dict:
    """读取世界基本信息，字段缺失时给默认值，绝不崩溃。

    返回键：name / version / game_type / difficulty。
    """
    try:
        data = File.load(world_path / "level.dat", gzipped=True)["Data"]
    except Exception:
        return {"name": None, "version": None, "game_type": None, "difficulty": None}
    return {
        "name": str(data.get("LevelName", "")),
        "version": int(data.get("version", 0)),
        "game_type": int(data.get("GameType", 0)),
        "difficulty": int(data.get("Difficulty", 0)),
    }


def list_world_files(world_path: Path) -> list[Path]:
    """递归列出世界目录下所有扫描目标文件（.dat/.mca/.json/.mcfunction）。"""
    return [p for p in sorted(world_path.rglob("*"))
            if p.is_file() and p.suffix.lower() in _SCAN_SUFFIXES]
