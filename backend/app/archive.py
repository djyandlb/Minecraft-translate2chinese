import zipfile
from pathlib import Path

def is_archive(path: Path) -> bool:
    """是否整合包压缩包（zip / mrpack）。"""
    return path.suffix.lower() in (".zip", ".mrpack")

def extract_modpack(archive: Path, dest: Path) -> Path:
    """解压整合包压缩包到 dest（自动建目录），返回解压根目录。
    zip 与 mrpack 同为 zip 容器；mrpack 根含 mods/、overrides/，解压后按普通目录扫。"""
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    except (zipfile.BadZipFile, ValueError) as e:
        # 损坏/非法 zip 容器：抛带路径的明确异常，便于上层定位坏文件
        raise ValueError(f"无法解压整合包 {archive}: {e}") from e
    return dest
