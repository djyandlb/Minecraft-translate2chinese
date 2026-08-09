# -*- coding: utf-8 -*-
"""M6 全文本覆盖扫描：文本源发现 + 写回（纯模块，不接 auto_flow）。

三类文本源：
  1. lang —— 语言文件 assets/*/lang/en_us.{json,lang,properties}（复用 jar/langfile，扩展 properties）
  2. json —— assets/ 下其他结构化 JSON（排除 lang 目录），递归找字符串值，技术串跳过
  3. lines —— en_us 路径段落下的 .txt/.md（Patchouli 等），逐行提取，行快照保留结构

发现阶段全程在 zip 层只读、不落盘，天然规避 zip-slip（不可信 jar 的恶意条目
根本不会写入磁盘）；写回阶段需要改动 jar 内文件，走「安全解压 → 改写 → 重打包」，
其中 _extract_jar 复用 hardcode._extract_jar 的 zip-slip 安全解压模式。

行快照思路参考方块译匠 localized-text.js 的 parseLocalizedTextSnapshot：
BOM 单独记录、EOL 取 \r\n 优先、trailing newline 单独记录、按行号定位替换。
只参考思路，不抄码（对方无开源许可证说明）。
"""

import json
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from app.jar import lang_files_from_namelist
from app.langfile import (
    parse_json_lang,
    parse_lang,
    parse_properties,
    write_json_lang,
    write_lang,
    write_properties,
)
from app.translate.common import should_translate


# ---------- 数据模型 ----------

@dataclass
class TextSource:
    """一类文本源：描述 jar 内一个可翻译文件及其目标写回路径。"""
    kind: str                 # "lang" | "json" | "lines"
    modid: str
    source_path: str          # jar 内相对路径，如 assets/mymod/lang/en_us.json
    target_path: str          # 替换 en_us → zh_cn 后的路径（无 en_us 段则等于 source_path）
    # kind=="lang"：entries 为 key→原文；kind=="json"：entries 为 key_path→原文
    # kind=="lines"：entries 为 line{i}→原文，行快照见 line_snapshot
    entries: dict[str, str] = field(default_factory=dict)
    line_snapshot: dict | None = None   # lines 专用：{bom, eol, trailing_newline, line_count}
    # 目录文本源（config/toml/properties/kubejs js）行内替换专用：
    # key → (line_idx, start, end[, quote])，渲染时只替换行内子串，保留结构
    spans: dict | None = None


# ---------- 路径工具 ----------

# 判断是否处于 assets/<modid>/lang/ 目录（该目录下的 json 属语言文件，不重复进结构化 JSON）
_LANG_DIR_RE = re.compile(r"^assets/[^/]+/lang/")


def _in_lang_dir(path: str) -> bool:
    return _LANG_DIR_RE.match(path) is not None


def _has_en_us_segment(path: str) -> bool:
    """路径是否含 en_us 段（如 patchouli 书 assets/*/patchouli_books/*/en_us/...）。"""
    return "en_us" in PurePosixPath(path).parts


def _zh_cn_path(path: str) -> str:
    """把路径中 en_us 替换为 zh_cn，保持扩展名；无 en_us 则原样返回。

    覆盖两种形态：独立段（patchouli 书 .../en_us/entries/...）与文件名前缀
    （语言文件 en_us.json → zh_cn.json）。
    """
    parts = PurePosixPath(path).parts
    replaced = tuple(
        ("zh_cn" + p[len("en_us"):]) if p == "en_us" or p.startswith("en_us") else p
        for p in parts
    )
    return str(PurePosixPath(*replaced))


# ---------- 行快照（lines 文本源） ----------

def _split_lines(text: str) -> tuple[list[str], dict]:
    """把文本拆成行数组 + 快照元数据（BOM/EOL/trailing_newline/line_count）。

    语义对齐方块译匠 parseLocalizedTextSnapshot：BOM 单独记录；EOL 优先识别 \r\n；
    trailing newline 单独记录。Python splitlines 在 trailing newline 时不会产出
    末尾空元素，因此 line_count 即有效行数。
    """
    bom = text.startswith("\ufeff")
    body = text[1:] if bom else text
    eol = "\r\n" if "\r\n" in body else "\n"
    trailing = body.endswith(("\r\n", "\n"))
    lines = body.splitlines()
    snapshot = {
        "bom": bom,
        "eol": eol,
        "trailing_newline": trailing,
        "line_count": len(lines),
    }
    return lines, snapshot


_LINE_KEY_RE = re.compile(r"^line(\d+)$")


def _lines_entries(lines: list[str]) -> dict[str, str]:
    """非空行提取为条目，key=line{索引}（空行/空白行跳过）。"""
    return {f"line{i}": line for i, line in enumerate(lines) if line.strip()}


def _line_index(key: str) -> int | None:
    m = _LINE_KEY_RE.match(key)
    return int(m.group(1)) if m else None


# ---------- 语言文件解析 ----------

def _parse_lang_entry(fmt: str, raw: str) -> dict[str, str]:
    """按格式解析语言文件内容：json 去注释、lang/properties 按 key=value。"""
    if fmt == "json":
        return parse_json_lang(raw)
    if fmt == "lang":
        return parse_lang(raw)
    return parse_properties(raw)


# ---------- 结构化 JSON 遍历 ----------

def _walk_json(node, prefix: str, entries: dict[str, str],
               translate=should_translate) -> None:
    """递归遍历 JSON，收集应翻译的字符串值；key_path 形如 title.text / pages[0].text。

    技术串跳过复用 app.translate.common.should_translate（现有行为：
    单字母词放行、snake_case 标识符如 iron_ingot 跳过；结构化 JSON 里
    单字母键属技术串，由 B 阶段 AI 判断兜底）。translate 可注入目录文本源
    的扩展过滤（_pack_should_translate：额外跳过开关配置字面量）。
    """
    if isinstance(node, dict):
        for k, v in node.items():
            child = f"{prefix}.{k}" if prefix else str(k)
            _walk_json(v, child, entries, translate)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            child = f"{prefix}[{i}]" if prefix else f"[{i}]"
            _walk_json(v, child, entries, translate)
    elif isinstance(node, str):
        if translate(node):
            entries[prefix] = node


_KEY_PATH_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def _split_key_path(key_path: str) -> list[str | int]:
    """把 key_path 解析为逐层定位段：'title.text' → ['title','text']；
    'pages[0].text' → ['pages', 0, 'text']。"""
    parts: list[str | int] = []
    for m in _KEY_PATH_RE.finditer(key_path):
        parts.append(int(m.group(2)) if m.group(2) is not None else m.group(1))
    return parts


def _set_path(node, key_path: str, value: str) -> None:
    """按 key_path 在 JSON 结构中定位并覆写字符串值。"""
    parts = _split_key_path(key_path)
    cur = node
    for part in parts[:-1]:
        cur = cur[part]
    cur[parts[-1]] = value


# ---------- 发现：zip 只读遍历（不落盘，规避 zip-slip） ----------

def discover_text_sources(jar: Path) -> list[TextSource]:
    """发现 jar 内三类文本源，按 (kind, modid, source_path) 排序返回。

    只读不落盘：全程在 zip 条目层面操作，恶意条目（../ 逃逸等）根本不会写入
    磁盘，无需落盘即天然规避 zip-slip。单个文件损坏只跳过该文件，不中断整包。
    """
    result: list[TextSource] = []
    try:
        with zipfile.ZipFile(jar) as zf:
            names = zf.namelist()

            # 1) 语言文件：assets/*/lang/en_us.{json,lang,properties}
            for info in lang_files_from_namelist(names):
                if info["lang"] != "en_us":
                    continue
                try:
                    raw = zf.read(info["path"]).decode("utf-8")
                    entries = _parse_lang_entry(info["format"], raw)
                except (UnicodeDecodeError, ValueError):
                    continue   # 单个语言文件坏编码/坏 json：跳过该文件
                if not entries:
                    continue   # 空语言文件无需翻译
                result.append(TextSource(
                    kind="lang",
                    modid=info["modid"],
                    source_path=info["path"],
                    target_path=_zh_cn_path(info["path"]),
                    entries=entries,
                ))

            # 2) 结构化 JSON：assets/ 下非 lang 目录的 .json
            for name in names:
                if not name.startswith("assets/") or not name.endswith(".json"):
                    continue
                if _in_lang_dir(name):
                    continue
                try:
                    raw = zf.read(name).decode("utf-8")
                    data = json.loads(raw)
                except (UnicodeDecodeError, ValueError):
                    continue   # 单个 json 损坏/非 json：跳过该文件
                entries: dict[str, str] = {}
                _walk_json(data, "", entries)
                if not entries:
                    continue
                result.append(TextSource(
                    kind="json",
                    modid=name.split("/")[1],
                    source_path=name,
                    target_path=_zh_cn_path(name),
                    entries=entries,
                ))

            # 3) en_us 路径 txt/md：逐行提取（非空行），行快照保留结构
            for name in names:
                if not name.endswith((".txt", ".md")):
                    continue
                if not _has_en_us_segment(name):
                    continue
                try:
                    raw = zf.read(name).decode("utf-8")
                except UnicodeDecodeError:
                    continue
                lines, snapshot = _split_lines(raw)
                entries = _lines_entries(lines)
                if not entries:
                    continue
                result.append(TextSource(
                    kind="lines",
                    modid=name.split("/")[1],
                    source_path=name,
                    target_path=_zh_cn_path(name),
                    entries=entries,
                    line_snapshot=snapshot,
                ))
    except zipfile.BadZipFile:
        return []   # 整个 jar 不是合法 zip：不发现任何文本源

    result.sort(key=lambda s: (s.kind, s.modid, s.source_path))
    return result


# ---------- 写回：安全解压 → 改写 → 重打包 ----------

def _extract_jar(jar: Path, work: Path) -> None:
    """把 jar 安全解压到 work（已存在先清空）。

    zip-slip 防护，复用 hardcode._extract_jar 的安全解压模式：条目名经
    PurePosixPath 规范化，含 .. 段、绝对路径、或解析后逃逸出 work 的条目
    一律跳过（不入盘），不整体拒绝 jar，保证单个恶意条目不中断流程。
    """
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    work_resolved = work.resolve()
    with zipfile.ZipFile(jar, "r") as zf:
        for name in zf.namelist():
            clean = PurePosixPath(name)
            if clean.is_absolute() or ".." in clean.parts:
                continue
            target = work.joinpath(*clean.parts)
            try:
                # 双保险：解析后必须仍在 work 内（防符号链接/规范化逃逸）
                target.resolve().relative_to(work_resolved)
            except ValueError:
                continue
            if name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _repack(work: Path, jar: Path) -> None:
    """把 work 目录重新打包为 jar，覆盖原 jar（调用方保证 jar 是副本）。

    资源翻译不碰字节码，签名文件原样保留（不做 hardcode 的签名跳过——
    本模块只改资源，签名失效不影响大多数 ModLoader 加载）。
    """
    with zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(work.rglob("*")):
            if p.is_dir():
                continue
            zf.write(p, p.relative_to(work).as_posix())


def _abs(work: Path, jar_rel: str) -> Path:
    """把 jar 内相对路径映射到解压目录下的绝对路径。"""
    return work.joinpath(*PurePosixPath(jar_rel).parts)


def _render_lang(work: Path, source: TextSource, translations: dict[str, str]) -> str:
    """重建语言文件内容：target 已存在则合并（保留已有译文），否则只含已翻译键。"""
    fmt = PurePosixPath(source.source_path).suffix.lstrip(".")
    if fmt not in ("json", "lang", "properties"):
        fmt = "json"
    tgt = _abs(work, source.target_path)
    data: dict[str, str] = {}
    if tgt.exists():
        data = _parse_lang_entry(fmt, tgt.read_bytes().decode("utf-8"))
    data.update(translations)
    if fmt == "lang":
        return write_lang(data)
    if fmt == "properties":
        return write_properties(data)
    return write_json_lang(data)


def _render_json(work: Path, source: TextSource, translations: dict[str, str]) -> str:
    """重建结构化 JSON：保留技术串与整体结构，按 key_path 覆写译文。

    以 target（若有）为基底，否则以 source 为基底——保证深层键路径可定位。
    key_path 在结构中不存在时跳过（调用方翻译了但结构里已无该键）。
    """
    tgt = _abs(work, source.target_path)
    if tgt.exists():
        data = json.loads(tgt.read_bytes().decode("utf-8"))
    else:
        data = json.loads(_abs(work, source.source_path).read_bytes().decode("utf-8"))
    for key, value in translations.items():
        try:
            _set_path(data, key, value)
        except (KeyError, IndexError, TypeError):
            continue
    return json.dumps(data, ensure_ascii=False, indent=2)


def _render_lines(work: Path, source: TextSource, translations: dict[str, str]) -> str:
    """按行快照重建 lines 文件：保留 BOM/EOL/trailing newline/行结构，只替换有译文的行。

    基底优先用 target（已有一轮译文则保留），否则用 source 原文行——
    未翻译行保留原文，不因缺翻译而丢行。目标行数不足时以空行补足到 source 行数。
    """
    src_raw = _abs(work, source.source_path).read_bytes().decode("utf-8")
    src_lines, _ = _split_lines(src_raw)
    tgt = _abs(work, source.target_path)
    if tgt.exists():
        base_lines, snap = _split_lines(tgt.read_bytes().decode("utf-8"))
    else:
        base_lines, snap = _split_lines(src_raw)
    while len(base_lines) < len(src_lines):
        base_lines.append("")
    for key, value in translations.items():
        idx = _line_index(key)
        if idx is not None and 0 <= idx < len(base_lines):
            base_lines[idx] = value
    body = snap["eol"].join(base_lines)
    return ("\ufeff" if snap["bom"] else "") + body + (snap["eol"] if snap["trailing_newline"] else "")


def write_translated(jar: Path, source: TextSource, translations: dict[str, str]) -> None:
    """把译文写回 jar 内 source.target_path 对应文件。

    修改发生在 jar 副本上（调用方保证）。流程：安全解压 → 改写目标文件 →
    重打包覆盖 jar。target_path 不存在时新建。translations 为空则直接返回。
    """
    if not translations:
        return
    work = jar.parent / f".{jar.stem}_ts"
    try:
        _extract_jar(jar, work)
        target = _abs(work, source.target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.kind == "lines":
            content = _render_lines(work, source, translations)
        elif source.kind == "json":
            content = _render_json(work, source, translations)
        else:
            content = _render_lang(work, source, translations)
        # 用字节写入：避开 Path.write_text 在 Windows 上默认把 \n 转 \r\n 的坑
        target.write_bytes(content.encode("utf-8"))
        _repack(work, jar)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------- 语言文件写回（modjar 单一汉化 jar 用） ----------

# modid 不可信：白名单 [a-z0-9_-]，不含 "."，杜绝 ".."、"/" 等路径穿越（与资源包一致）
_MODID_RE = re.compile(r"[a-z0-9_-]+")

# 语言文件支持的格式
_LANG_FMTS = ("json", "lang", "properties")


def _lang_ext(work: Path, by_mod: dict[str, dict[str, str]], target_lang: str,
              pack_format: int) -> str:
    """推断目标语言文件格式：优先 jar 内已有目标语言文件，其次已有源语言文件，最后按 pack_format。"""
    for modid in by_mod:
        lang_dir = work / "assets" / modid / "lang"
        if not lang_dir.is_dir():
            continue
        for f in sorted(lang_dir.iterdir()):
            if f.stem == target_lang and f.suffix.lstrip(".") in _LANG_FMTS:
                return f.suffix.lstrip(".")
    for modid in by_mod:
        lang_dir = work / "assets" / modid / "lang"
        if not lang_dir.is_dir():
            continue
        for f in sorted(lang_dir.iterdir()):
            if f.suffix.lstrip(".") in _LANG_FMTS:
                return f.suffix.lstrip(".")
    # 兜底：pack_format ≥ 4（1.13+）用 .json，1.12 及以下用 .lang
    from app.version import pack_format_to_lang_ext
    return pack_format_to_lang_ext(pack_format)


def _serialize_lang(ext: str, data: dict[str, str]) -> str:
    """按格式序列化语言文件内容（json/lang/properties）。"""
    if ext == "lang":
        return write_lang(data)
    if ext == "properties":
        return write_properties(data)
    return write_json_lang(data)


def write_lang_into_jar(jar: Path, by_mod: dict[str, dict[str, str]], target_lang: str,
                        pack_format: int) -> None:
    """把 {modid: {key: 译文}} 语言文件翻译写回 jar（modjar 单一汉化 jar 用）。

    修改发生在 jar 副本上（调用方保证）。流程：安全解压 → 逐 modid 写
    assets/<modid>/lang/<target>.<ext>（目标语言文件已存在则合并已有条目）→ 重打包。
    格式沿用 jar 内已有目标/源语言文件，否则按 pack_format 推断（json/lang）。
    modid 走白名单校验，拦截路径穿越；by_mod 为空则直接返回。
    """
    if not by_mod:
        return
    work = jar.parent / f".{jar.stem}_lang"
    try:
        _extract_jar(jar, work)
        ext = _lang_ext(work, by_mod, target_lang, pack_format)
        for modid, entries in by_mod.items():
            if not entries or not _MODID_RE.fullmatch(modid):
                continue
            lang_dir = work / "assets" / modid / "lang"
            lang_dir.mkdir(parents=True, exist_ok=True)
            target = lang_dir / f"{target_lang}.{ext}"
            data: dict[str, str] = {}
            if target.exists():
                data = _parse_lang_entry(ext, target.read_bytes().decode("utf-8"))
            data.update(entries)
            # 用字节写入：避开 Path.write_text 在 Windows 上默认把 \n 转 \r\n 的坑
            target.write_bytes(_serialize_lang(ext, data).encode("utf-8"))
        _repack(work, jar)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------- 整合包目录文本源（任务线 / config / data / kubejs） ----------

# config 可见文本：json/toml/properties；data 数据包：json；kubejs/scripts：js 字符串字面量
_PACK_JSON_PREFIXES = ("config/", "data/")
_PACK_JS_PREFIXES = ("kubejs/", "scripts/")

# 配置开关/布尔字面量：不翻译（true/false/on/off 等），避免把布尔配置翻译坏
_SWITCH_LITERALS = {
    "true", "false", "yes", "no", "on", "off", "null", "none",
    "enabled", "disabled", "enable", "disable",
}


def _pack_should_translate(text: str) -> bool:
    """目录文本源过滤：should_translate + 配置开关字面量跳过（布尔/开关配置不翻译）。"""
    if not should_translate(text):
        return False
    return text.strip().lower() not in _SWITCH_LITERALS


def _pack_modid(rel: str) -> str:
    """目录文本源的 modid 元数据：data/ 下取 namespace，其他取路径第一段。"""
    parts = rel.split("/")
    if parts[0] == "data" and len(parts) > 2:
        return parts[1]
    return parts[0]


def _pack_json_source(root: Path, p: Path, rel: str) -> TextSource | None:
    """config/data 下 json：递归字符串值，key_path 定位；技术串/开关值过滤。"""
    data = json.loads(p.read_bytes().decode("utf-8"))
    entries: dict[str, str] = {}
    _walk_json(data, "", entries, _pack_should_translate)
    if not entries:
        return None
    return TextSource(kind="json", modid=_pack_modid(rel),
                      source_path=rel, target_path=rel, entries=entries)


# 单行 `key = "value"` 字符串值（忽略数组/多行字符串/数字/布尔）
_TOML_STR_RE = re.compile(r'^\s*[A-Za-z0-9_.-]+\s*=\s*"((?:\\.|[^"\\])*)"\s*(?:#.*)?$')


def _pack_toml_source(root: Path, p: Path, rel: str) -> TextSource | None:
    """config 下 toml：单行字符串值提取，行内偏移替换（保留 key 与格式）。"""
    raw = p.read_bytes().decode("utf-8")
    lines, snapshot = _split_lines(raw)
    entries: dict[str, str] = {}
    spans: dict[str, tuple[int, int, int]] = {}
    for i, line in enumerate(lines):
        m = _TOML_STR_RE.match(line)
        if not m:
            continue
        val = m.group(1)
        if _pack_should_translate(val):
            key = f"line{i}"
            entries[key] = val
            spans[key] = (i, m.start(1), m.end(1))
    if not entries:
        return None
    return TextSource(kind="lines", modid=_pack_modid(rel), source_path=rel, target_path=rel,
                      entries=entries, line_snapshot=snapshot, spans=spans)


def _pack_properties_source(root: Path, p: Path, rel: str) -> TextSource | None:
    """config 下 properties：key=value 值提取，行内偏移替换（保留 key= 前缀）。"""
    raw = p.read_bytes().decode("utf-8")
    lines, snapshot = _split_lines(raw)
    entries: dict[str, str] = {}
    spans: dict[str, tuple[int, int, int]] = {}
    for i, line in enumerate(lines):
        eq = line.find("=")
        if eq < 0:
            continue
        key = line[:eq].strip()
        val = line[eq + 1:].strip()
        if not key or not val:
            continue
        start = eq + 1
        while start < len(line) and line[start].isspace():
            start += 1
        if _pack_should_translate(val):
            k = f"line{i}"
            entries[k] = val
            spans[k] = (i, start, len(line))
    if not entries:
        return None
    return TextSource(kind="lines", modid=_pack_modid(rel), source_path=rel, target_path=rel,
                      entries=entries, line_snapshot=snapshot, spans=spans)


# js 字符串字面量：双引号/单引号，允许转义序列
_JS_STR_LITERAL_RE = re.compile(r'(?P<quote>["\'])(?P<body>(?:\\.|[^\\])*?)(?P=quote)')

_JS_UNESCAPE_MAP = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "'": "'"}


def _js_unescape(body: str) -> str:
    """js 字符串字面量反转义（常见转义序列），未知转义保留原样。"""
    def _rep(m):
        return _JS_UNESCAPE_MAP.get(m.group(1), m.group(1))
    return re.sub(r"\\(.)", _rep, body)


def _js_escape(text: str, quote: str) -> str:
    """按引号类型转义译文，保证写入 js 字符串字面量语法正确。"""
    return (text.replace("\\", "\\\\").replace(quote, "\\" + quote)
            .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))


def _pack_js_source(root: Path, p: Path, rel: str) -> TextSource | None:
    """kubejs/scripts 下 js：字符串字面量内容提取，行内偏移替换（保留结构）。"""
    raw = p.read_bytes().decode("utf-8")
    lines, snapshot = _split_lines(raw)
    entries: dict[str, str] = {}
    spans: dict[str, tuple[int, int, int, str]] = {}
    idx = 0
    for li, line in enumerate(lines):
        for m in _JS_STR_LITERAL_RE.finditer(line):
            quote = m.group("quote")
            content = _js_unescape(m.group("body"))
            if _pack_should_translate(content):
                key = f"l{idx}"
                entries[key] = content
                spans[key] = (li, m.start("body"), m.end("body"), quote)
            idx += 1
    if not entries:
        return None
    return TextSource(kind="lines", modid=_pack_modid(rel), source_path=rel, target_path=rel,
                      entries=entries, line_snapshot=snapshot, spans=spans)


def discover_pack_text_sources(pack_dir: Path) -> list[TextSource]:
    """发现整合包目录（非 jar）文本源，复用 TextSource，source_path=整合包相对路径。

    覆盖：任务线 config/ftbquests/quests/*.json、config/**/*.{json,toml,properties}、
    data/**/*.json、kubejs/ 与 scripts/ 下 *.js 字符串字面量。
    技术串/开关配置值过滤（_pack_should_translate）。只读不写，原整合包不被改动；
    单个文件坏编码/坏格式只跳过该文件，不中断整包。
    """
    root = Path(pack_dir)
    result: list[TextSource] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            continue
        suffix = p.suffix.lower()
        try:
            if suffix == ".json" and rel.startswith(_PACK_JSON_PREFIXES):
                src = _pack_json_source(root, p, rel)
            elif suffix == ".toml" and rel.startswith("config/"):
                src = _pack_toml_source(root, p, rel)
            elif suffix == ".properties" and rel.startswith("config/"):
                src = _pack_properties_source(root, p, rel)
            elif suffix == ".js" and rel.startswith(_PACK_JS_PREFIXES):
                src = _pack_js_source(root, p, rel)
            else:
                src = None
            if src:
                result.append(src)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError, OSError):
            continue   # 单个文件坏编码/坏 json：跳过该文件
    result.sort(key=lambda s: (s.kind, s.source_path))
    return result


def render_pack_source(source: TextSource, translations: dict[str, str], pack_dir: Path) -> str:
    """渲染目录文本源译文内容（读原文件，不改原包），供汉化补丁包写入。

    json：key_path 覆写，保留结构与技术串；lines：优先行内子串替换（spans），
    否则整行替换。返回渲染后的文件内容字符串。
    """
    src_file = Path(pack_dir).joinpath(*PurePosixPath(source.source_path).parts)
    raw = src_file.read_bytes().decode("utf-8")
    if source.kind == "json":
        data = json.loads(raw)
        for key, value in translations.items():
            try:
                _set_path(data, key, value)
            except (KeyError, IndexError, TypeError):
                continue
        return json.dumps(data, ensure_ascii=False, indent=2)
    lines, snapshot = _split_lines(raw)
    for key, value in translations.items():
        span = (source.spans or {}).get(key)
        if span:
            li, start, end = span[0], span[1], span[2]
            if len(span) >= 4 and span[3]:
                value = _js_escape(value, span[3])   # js 字符串字面量：译文转义
            if 0 <= li < len(lines) and 0 <= start <= end <= len(lines[li]):
                line = lines[li]
                lines[li] = line[:start] + value + line[end:]
        else:
            idx = _line_index(key)
            if idx is not None and 0 <= idx < len(lines):
                lines[idx] = value
    body = snapshot["eol"].join(lines)
    return ("\ufeff" if snapshot["bom"] else "") + body + (snapshot["eol"] if snapshot["trailing_newline"] else "")
