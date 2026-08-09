import re
import zipfile
from pathlib import Path

# 匹配 assets/<modid>/lang/<lang>.(json|lang|properties)，大小写不敏感：
# en_US / EN_us / En_us 等语言文件一律识别（实测不少 mod 用大写语言码），
# 提取后 lang/format 统一小写，与 source_lang（en_us 等）及格式判断对齐。
# 扩展 properties：部分 mod（含 Java 系）语言文件用 .properties 存储
_LANG_RE = re.compile(r"^assets/([^/]+)/lang/([a-zA-Z0-9_]+)\.(json|lang|properties)$", re.IGNORECASE)


def lang_files_from_namelist(names: list[str]) -> list[dict]:
    """从 zip namelist 提取语言文件条目（纯函数，供重复扫描复用，避免二次开 zip）。"""
    result: list[dict] = []
    for name in names:
        m = _LANG_RE.match(name)
        if m:
            result.append({
                "path": name,
                "modid": m.group(1),
                "lang": m.group(2).lower(),
                "format": m.group(3).lower(),
            })
    return result


def list_jar_lang_files(jar_path: Path) -> list[dict]:
    """枚举 jar 内所有语言文件条目，每条含 path/modid/lang/format。"""
    with zipfile.ZipFile(jar_path) as zf:
        return lang_files_from_namelist(zf.namelist())


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
