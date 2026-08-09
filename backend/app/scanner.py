import json
import sys
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from app.jar import lang_files_from_namelist
from app.langfile import parse_lang, parse_json_lang, parse_properties


@dataclass
class ModScan:
    jar_path: Path
    modid: str
    source_entries: dict[str, str]
    target_entries: dict[str, str] = field(default_factory=dict)
    lang_format: str = "json"


def _read_entries(zf: zipfile.ZipFile, path: str, fmt: str) -> dict[str, str]:
    """从 zip 内读语言文件：json 去注释、lang/properties 按 key=value。"""
    raw = zf.read(path).decode("utf-8")
    if fmt == "lang":
        return parse_lang(raw)
    if fmt == "properties":
        return parse_properties(raw)
    return parse_json_lang(raw)


def _scan_one_jar(jar: Path, source_lang: str, target_lang: str) -> list[ModScan]:
    """解析单个 jar 内所有 modid 的语言文件（一 jar 可能含多 modid）。

    容错：单个 jar 损坏（非 zip、语言文件 json 语法错误、非法编码）只跳过该 jar，
    不中断整个整合包扫描。json 异常来自 parse_json_lang（langfile.py），在扫描层统一兜底。
    """
    try:
        results: list[ModScan] = []
        with zipfile.ZipFile(jar) as zf:
            for info in lang_files_from_namelist(zf.namelist()):
                if info["lang"] != source_lang:
                    continue
                tgt_path = f"assets/{info['modid']}/lang/{target_lang}.{info['format']}"
                src = _read_entries(zf, info["path"], info["format"])
                tgt = _read_entries(zf, tgt_path, info["format"]) if tgt_path in zf.namelist() else {}
                results.append(ModScan(jar_path=jar, modid=info["modid"],
                                       source_entries=src, target_entries=tgt,
                                       lang_format=info["format"]))
        return results
    except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError,
            zlib.error, EOFError) as e:
        # 损坏的 jar / 语言文件 / 压缩流异常（zlib.error、EOFError）：跳过该 mod，
        # 不让一个坏文件炸掉整包扫描
        print(f"[警告] 跳过损坏的 mod: {jar} - {e}", file=sys.stderr)
        return []


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
