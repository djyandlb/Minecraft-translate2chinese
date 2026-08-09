import re
import zipfile
from pathlib import Path

# 匹配 assets/<modid>/lang/<lang>.(json|lang)，modid/lang 均为小写字母数字下划线
_LANG_RE = re.compile(r"^assets/([^/]+)/lang/([a-z0-9_]+)\.(json|lang)$")


def list_jar_lang_files(jar_path: Path) -> list[dict]:
    """枚举 jar 内所有语言文件条目，每条含 path/modid/lang/format。"""
    result: list[dict] = []
    with zipfile.ZipFile(jar_path) as zf:
        for name in zf.namelist():
            m = _LANG_RE.match(name)
            if m:
                result.append({
                    "path": name,
                    "modid": m.group(1),
                    "lang": m.group(2),
                    "format": m.group(3),
                })
    return result


def extract_jar_to(jar_path: Path, out_dir: Path) -> None:
    """解压 jar 到 out_dir。"""
    with zipfile.ZipFile(jar_path) as zf:
        zf.extractall(out_dir)


def pack_dir_to_jar(src_dir: Path, jar_path: Path) -> None:
    """把目录重新打成 jar（zip），保持相对路径。"""
    with zipfile.ZipFile(jar_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(src_dir.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(src_dir).as_posix())
