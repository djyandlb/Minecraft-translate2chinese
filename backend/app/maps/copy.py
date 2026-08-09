import shutil
from pathlib import Path

def copy_world(world_path: Path, dest: Path) -> Path:
    """整档复制到副本 dest（已存在先删）。原档只读，后续操作全在副本。"""
    if not world_path.exists():
        raise FileNotFoundError(f"世界不存在: {world_path}")
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(world_path, dest)
    return dest
