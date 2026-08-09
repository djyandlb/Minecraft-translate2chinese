"""M4-5 地图导出模块。

把汉化副本目录打成 mcworld zip（与 MCC-i18n 同思路）。
全程 pathlib 路径，保留相对路径，不碰原档。
"""
import zipfile
from pathlib import Path


def export_world(src_world: Path, out_zip: Path) -> Path:
    """把汉化副本打成 mcworld zip（保留相对路径）。"""
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(src_world.rglob("*")):
            if f.is_file() and f.name.endswith(".bak"):
                continue   # M4-recheck：跳过写回生成的 <name>.bak 备份，避免污染 mcworld 包
            zf.write(f, f.relative_to(src_world).as_posix())
    return out_zip
