import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from app.jar import list_jar_lang_files
from app.langfile import parse_lang, parse_json_lang


@dataclass
class ModScan:
    jar_path: Path
    modid: str
    source_entries: dict[str, str]
    target_entries: dict[str, str] = field(default_factory=dict)
    lang_format: str = "json"


def _read_entries(zf: zipfile.ZipFile, path: str, fmt: str) -> dict[str, str]:
    """从 zip 内读语言文件：json 经 parse_json_lang 去注释，lang 按 key=value。"""
    raw = zf.read(path).decode("utf-8")
    if fmt == "lang":
        return parse_lang(raw)
    return parse_json_lang(raw)


def _scan_one_jar(jar: Path, source_lang: str, target_lang: str) -> list[ModScan]:
    """解析单个 jar 内所有 modid 的语言文件（一 jar 可能含多 modid）。"""
    results: list[ModScan] = []
    with zipfile.ZipFile(jar) as zf:
        for info in list_jar_lang_files(jar):
            if info["lang"] != source_lang:
                continue
            tgt_path = f"assets/{info['modid']}/lang/{target_lang}.{info['format']}"
            src = _read_entries(zf, info["path"], info["format"])
            tgt = _read_entries(zf, tgt_path, info["format"]) if tgt_path in zf.namelist() else {}
            results.append(ModScan(jar_path=jar, modid=info["modid"],
                                   source_entries=src, target_entries=tgt,
                                   lang_format=info["format"]))
    return results


def scan_jar(jar_path: Path, source_lang: str, target_lang: str) -> list[ModScan]:
    return _scan_one_jar(jar_path, source_lang, target_lang)


def scan_modpack(dir: Path, source_lang: str, target_lang: str, scope: str = "mods") -> list[ModScan]:
    """扫描整合包目录。scope="mods" 仅扫 mods/**/*.jar；"all" 全目录递归。"""
    results: list[ModScan] = []
    root = dir / "mods" if scope == "mods" else dir
    if not root.exists():
        return results
    for jar in sorted(root.rglob("*.jar")):
        results.extend(_scan_one_jar(jar, source_lang, target_lang))
    return results
