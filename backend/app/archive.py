import zipfile
from pathlib import Path

def is_archive(path: Path) -> bool:
    """是否整合包压缩包（zip / mrpack）。"""
    return path.suffix.lower() in (".zip", ".mrpack")

def extract_modpack(archive: Path, dest: Path) -> Path:
    """解压整合包压缩包到 dest（自动建目录），返回解压根目录。
    zip 与 mrpack 同为 zip 容器；mrpack 根含 mods/、overrides/，解压后按普通目录扫。"""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(dest)
    return dest
