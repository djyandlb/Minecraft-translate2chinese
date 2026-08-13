# -*- coding: utf-8 -*-
"""M6 全文本覆盖扫描：文本源发现 + 写回（纯模块，不接 auto_flow）。

三类文本源：
  1. lang —— 语言文件 assets/*/lang/en_us.{json,lang,properties}（复用 jar/langfile，扩展 properties）
  2. json —— assets/ 下其他结构化 JSON（排除 lang 目录），递归找字符串值，技术串跳过
  3. lines —— en_us 路径段落下的 .txt/.md（Patchouli 等），逐行提取，行快照保留结构

发现阶段全程在 zip 层只读、不落盘，天然规避 zip-slip（不可信 jar 的恶意条目
根本不会写入磁盘）；写回阶段需要改动 jar 内文件，走「安全解压 → 改写 → 重打包」，
其中 _extract_jar 复用 hardcode._extract_jar 的 zip-slip 安全解压模式。

行快照：BOM 单独记录、EOL 取 \r\n 优先、trailing newline 单独记录、按行号定位替换，
翻译后按行号回写目标文件，保留原有行结构。
"""

import json
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from app.jar import lang_files_from_namelist
from app.langfile import (
    lang_value_ok,
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


# Minecraft 动画 easing 枚举（Blockbench/vanilla animation format 的引擎关键字）：
# linear/smooth/step/bounce/catmullrom + easeIn/easeOut/easeInOut（camelCase）及其
# snake_case 变体。这些是动画插值函数的**枚举名不是正文**，翻译成「线性/回弹」会让
# 动画加载器读不到该枚举 → 动画坏（用户实测 animations easing 被翻成「线性/回弹」）。
_EASING_KEYWORDS = {
    "linear", "smooth", "step", "bounce", "catmullrom",
    "easein", "easeout", "easeinout",
    "ease_in", "ease_out", "ease_in_out",
}


def _json_should_translate(text: str) -> bool:
    """结构化 JSON 值判定：动画 easing 枚举精确命中 → 跳过（保留原文）。
    只在值整体是单个枚举词时跳过（防误伤普通文本里的 linear/step 等词）。
    max_len 放宽到 5000：本过滤只用于明确文本载体（Patchouli 教程书 text / advancements
    description）——长教程正文（>1000 字符）修复 recheck 前被当超长漏提。"""
    if text.strip().lower() in _EASING_KEYWORDS:
        return False
    return should_translate(text, max_len=5000)


def _is_patchouli_json(path: str) -> bool:
    """Patchouli 帕秋莉教程书：assets/*/patchouli_books/*/en_us/...（剧情/教程正文）。"""
    p = PurePosixPath(path)
    return "patchouli_books" in p.parts and "en_us" in p.parts


def _is_advancement_json(path: str) -> bool:
    """advancements 进度：data/<namespace>/advancements/**/*.json（title/description 可翻）。"""
    p = PurePosixPath(path)
    return bool(p.parts) and p.parts[0] == "data" and "advancements" in p.parts


# advancements 只收 display.title/description 的文本（直接字符串或 text/translate 嵌套）。
# 不收 criteria（触发条件键名/值）等代码逻辑——用户实测 criteria.throw_ring_in_lava 被翻成「这个」。
_ADV_TEXT_PATHS = {
    "display.title", "display.title.text", "display.title.translate",
    "display.description", "display.description.text", "display.description.translate",
}


def _is_text_carrier_json(path: str) -> bool:
    """结构化 JSON 是否为「明确文本载体」（可翻译）——回归 Minecraft 汉化标准：
    只翻 lang + 教程书 + 进度，其余程序资源/配置 json（模型/材质/动画/配方/战利品/
    config 参数）翻译必坏，且是大整合包十几万条噪音条目的根源（用户反馈 debug 难受）。"""
    return _is_patchouli_json(path) or _is_advancement_json(path)


def _is_pack_text_carrier(rel: str) -> bool:
    """整合包目录 json 白名单（回归标准）：只翻明确文本载体——任务书/进度/语言文件。
    其余 config/data 的 json 是配置/数据（参数/坐标/路径），翻译必坏且噪音大。
    修复（recheck，用户实测）：放开**任意 assets/<mod>/lang/**（非只 kubejs/assets）——
    整合包自定义资源包/自带 lang（含 FTBQ 翻译键 ftbquestlocalizer、Create ponder 键等）
    全收，否则这些用户可见文本漏翻。"""
    p = PurePosixPath(rel)
    if "assets" in p.parts and "lang" in p.parts:
        return True    # 语言文件 assets/<mod>/lang/en_us.*（含 kubejs/assets、自定义资源包）
    if "patchouli_books" in p.parts and "en_us" in p.parts:
        return True    # Patchouli 教程书
    if rel.startswith("config/ftbquests/") or rel.startswith("config/betterquesting/"):
        return True    # FTB Quests / Better Questing 任务书（剧情主线）
    if bool(p.parts) and p.parts[0] == "data" and "advancements" in p.parts:
        return True    # advancements 进度（title/description）
    return False


def _has_en_us_segment(path: str) -> bool:
    """路径是否含 en_us 段（如 patchouli 书 assets/*/patchouli_books/*/en_us/...）。"""
    return "en_us" in PurePosixPath(path).parts


def _zh_cn_path(path: str, target_lang: str = "zh_cn") -> str:
    """把路径中 en_us 替换为目标语言代码（默认 zh_cn），保持扩展名；无 en_us 则原样返回。

    覆盖两种形态：独立段（patchouli 书 .../en_us/entries/...）与文件名前缀
    （语言文件 en_us.json → <target_lang>.json）。
    修复（recheck）：原硬编码 zh_cn——繁体目标（zh_tw）时译文仍写 zh_cn/ 目录，
    Patchouli 在繁体环境只找 zh_tw/，找不到就回退 en_us，译文不生效。
    """
    parts = PurePosixPath(path).parts
    replaced = tuple(
        (target_lang + p[len("en_us"):]) if p == "en_us" or p.startswith("en_us") else p
        for p in parts
    )
    return str(PurePosixPath(*replaced))


def _is_guide_md(path: str) -> bool:
    """GuideME 类指南 markdown：assets/<mod>/{ae2guide,guidebook,guide}/...（AE2 指南等）。
    无 en_us 路径段（_has_en_us_segment 判不过），但指南页面是用户可见文本——需汉化，
    产物走 `_<locale>` 镜像目录（GuideME 按 _<locale> 加载多语言）。"""
    return path.endswith((".md", ".txt")) and any(
        s in ("ae2guide", "guidebook", "guide") for s in PurePosixPath(path).parts)


def _guide_mirror_path(path: str, target_lang: str = "zh_cn") -> str:
    """GuideME 指南镜像路径：assets/<mod>/ae2guide/<f>.md → assets/<mod>/ae2guide/_<lang>/<f>.md。
    GuideME 按 `_<locale>` 子目录加载多语言（AE2 指南），资源包覆盖该镜像即可生效。"""
    parts = list(PurePosixPath(path).parts)
    for i, seg in enumerate(parts):
        if seg in ("ae2guide", "guidebook", "guide") and i + 1 < len(parts):
            parts.insert(i + 1, f"_{target_lang}")
            return str(PurePosixPath(*parts))
    return path


# ---------- 行快照（lines 文本源） ----------

def _split_lines(text: str) -> tuple[list[str], dict]:
    """把文本拆成行数组 + 快照元数据（BOM/EOL/trailing_newline/line_count）。

    行快照：BOM 单独记录；EOL 优先识别 \r\n；
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
    """非空行提取为条目，key=line{索引}（空行/空白行跳过）。

    修复（recheck）：动画 easing 枚举整值（linear/step/bounce 等，与 json/pack 源
    的 _json_should_translate/_pack_should_translate 白名单一致）不收集——否则
    .txt/.md 里独立成行的 easing 值被翻译成「线性/回弹」破坏动画示例。"""
    out: dict[str, str] = {}
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        if s.lower() in _EASING_KEYWORDS:
            continue
        out[f"line{i}"] = line
    return out


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
               translate=should_translate, allow_keys: set[str] | None = None) -> None:
    """递归遍历 JSON，收集应翻译的字符串值；key_path 形如 title.text / pages[0].text。

    技术串跳过复用 app.translate.common.should_translate（现有行为：
    单字母词放行、snake_case 标识符如 iron_ingot 跳过；结构化 JSON 里
    单字母键属技术串，由 B 阶段 AI 判断兜底）。translate 可注入目录文本源
    的扩展过滤（_pack_should_translate：额外跳过开关配置字面量）。
    allow_keys：非 None 时只收 key_path **精确等于**白名单的值（如 advancements
    只收 display.title/description 文本）——防止 criteria 等代码逻辑字段被当文本
    翻译（用户实测 criteria.throw_ring_in_lava 被翻成「这个」）。
    """
    if isinstance(node, dict):
        for k, v in node.items():
            child = f"{prefix}.{k}" if prefix else str(k)
            _walk_json(v, child, entries, translate, allow_keys)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            child = f"{prefix}[{i}]" if prefix else f"[{i}]"
            _walk_json(v, child, entries, translate, allow_keys)
    elif isinstance(node, str):
        if translate(node) and (allow_keys is None or prefix in allow_keys):
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

def discover_text_sources(jar: Path, target_lang: str = "zh_cn") -> list[TextSource]:
    """发现 jar 内三类文本源，按 (kind, modid, source_path) 排序返回。

    只读不落盘：全程在 zip 条目层面操作，恶意条目（../ 逃逸等）根本不会写入
    磁盘，无需落盘即天然规避 zip-slip。单个文件损坏只跳过该文件，不中断整包。
    target_lang：译文目标语言，用于把 en_us 路径替换为对应语言（繁体目标 zh_tw 时
    生成 zh_tw/ 路径，Patchouli 等才加载得到）。
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
                    raw = zf.read(info["path"]).decode("utf-8-sig")
                    entries = _parse_lang_entry(info["format"], raw)
                except (UnicodeDecodeError, ValueError):
                    continue   # 单个语言文件坏编码/坏 json：跳过该文件
                # 语言文件值宽松过滤（长度 2-200 + 含字母），不走 should_translate：
                # "Requires_Armor" 这类 snake_case 真实短语必须放行；只滤过短/纯数字/纯符号
                entries = {k: v for k, v in entries.items() if lang_value_ok(v)}
                if not entries:
                    continue   # 过滤后空语言文件无需翻译
                result.append(TextSource(
                    kind="lang",
                    modid=info["modid"],
                    source_path=info["path"],
                    target_path=_zh_cn_path(info["path"], target_lang),
                    entries=entries,
                ))

            # 2) 结构化 JSON：只翻「明确文本载体」（回归 Minecraft 汉化标准）。
            #    全文本覆盖（把所有 assets/data 的 json 都扫进翻译）是反标准的——
            #    程序资源 json（模型/材质/动画/blockstate/粒子/配方/战利品表）翻译必坏
            #    （easing→「线性/回弹」动画坏），且大整合包十几万条大部分是这种噪音，
            #    用户 debug 极难受。这里只翻 Patchouli 教程书（assets/）与
            #    advancements 进度（data/），其余 json 一律不碰。
            for name in names:
                if not name.endswith(".json"):
                    continue
                if _in_lang_dir(name):
                    continue
                if not _is_text_carrier_json(name):
                    continue
                try:
                    raw = zf.read(name).decode("utf-8-sig")
                    data = json.loads(raw)
                except (UnicodeDecodeError, ValueError):
                    continue   # 单个 json 损坏/非 json：跳过该文件
                entries: dict[str, str] = {}
                # easing 枚举白名单（_json_should_translate）：教程书/进度里散落的
                # 枚举值（linear/step/bounce 等）也拦截，产物保留原文。
                # 修复（recheck）：advancements 只收 display.title/description 文本，
                # 不收 criteria 触发条件（代码逻辑，翻译会破坏进度判定——用户实测被翻成「这个」）
                _walk_json(data, "", entries, _json_should_translate,
                           allow_keys=_ADV_TEXT_PATHS if _is_advancement_json(name) else None)
                if not entries:
                    continue
                # 修复（recheck）：mod 已自带目标语言版本（patchouli 书 zh_cn/ 目录等）→ 跳过，
                # 不重复提取翻译自带中文 mod（用户质疑「本就适配中文的 mod 别胡扯重翻」）。
                # 仅当目标路径确实变化（含 en_us 段）才检查——advancements（data/）无 en_us 段
                # 目标路径相同，不跳过
                _target = _zh_cn_path(name, target_lang)
                if _target != name and _target in names:
                    continue
                result.append(TextSource(
                    kind="json",
                    modid=name.split("/")[1],
                    source_path=name,
                    target_path=_target,
                    entries=entries,
                ))

            # 3) txt/md：逐行提取（非空行），行快照保留结构。收两类：
            #    a) 含 en_us 路径段（Patchouli 书等）→ _zh_cn_path 替换
            #    b) GuideME 指南目录（ae2guide/guidebook/guide，无 en_us 段，AE2 指南等）
            #       → _guide_mirror_path 生成 _<lang> 镜像（修复 recheck：AE2 指南 md 未汉化）
            for name in names:
                if not name.endswith((".txt", ".md")):
                    continue
                is_en_us = _has_en_us_segment(name)
                is_guide = _is_guide_md(name)
                if not is_en_us and not is_guide:
                    continue
                try:
                    # lines 保留 utf-8（不剥 BOM）——_split_lines 检测 ﻿ 记入快照，
                    # 写回时原样保留（快照结构还原）
                    raw = zf.read(name).decode("utf-8")
                except UnicodeDecodeError:
                    continue
                lines, snapshot = _split_lines(raw)
                entries = _lines_entries(lines)
                if not entries:
                    continue
                # 已有目标语言版本（zh_cn/ 或 _zh_cn/ 镜像）跳过，不重翻自带中文 mod。
                # guide（无 en_us 段）走 _<lang> 镜像目录（GuideME 约定）
                _target = (_zh_cn_path(name, target_lang) if is_en_us
                           else _guide_mirror_path(name, target_lang))
                if _target != name and _target in names:
                    continue
                result.append(TextSource(
                    kind="lines",
                    modid=name.split("/")[1],
                    source_path=name,
                    target_path=_target,
                    entries=entries,
                    line_snapshot=snapshot,
                ))
    except zipfile.BadZipFile:
        return []   # 整个 jar 不是合法 zip：不发现任何文本源

    result.sort(key=lambda s: (s.kind, s.modid, s.source_path))
    return result


# ---------- 写回：安全解压 → 改写 → 重打包 ----------

_WINDOWS_BAD_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_windows_entry(name: str) -> str:
    """zip 条目名 Windows 文件系统安全化（与 archive/hardcode 同款）。

    脏条目名（NeoForge data 标签）尾随空格 / `<>:"|?*` 控制字符在 Windows 上
    mkdir/open 抛 WinError，中断整个 jar 写回。逐段去首尾空白/尾随点、非法字符替换 _。
    """
    return _WINDOWS_BAD_CHARS_RE.sub("_", (name or "").strip().rstrip(". "))


def _extract_jar(jar: Path, work: Path) -> None:
    """把 jar 安全解压到 work（已存在先清空）。

    zip-slip 防护，复用 hardcode._extract_jar 的安全解压模式：条目名经
    PurePosixPath 规范化，含 .. 段、绝对路径、或解析后逃逸出 work 的条目
    一律跳过（不入盘），不整体拒绝 jar，保证单个恶意条目不中断流程。
    修复（recheck）：补 Windows 非法文件名清理（NeoForge data 标签脏条目名尾随空格/
    非法字符，Windows mkdir/open 抛 WinError 中断整个 jar 写回——hardcode 已修、这里漏网）。
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
            # Windows 非法字符/尾随空格清理（逐段替换，全非法段跳过）
            safe_parts = tuple(_sanitize_windows_entry(p) for p in clean.parts)
            if not safe_parts or any(not p for p in safe_parts):
                continue
            target = work.joinpath(*safe_parts)
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
        data = _parse_lang_entry(fmt, tgt.read_bytes().decode("utf-8-sig"))
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
        data = json.loads(tgt.read_bytes().decode("utf-8-sig"))
    else:
        data = json.loads(_abs(work, source.source_path).read_bytes().decode("utf-8-sig"))
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


def render_jar_source(jar: Path, source: TextSource, translations: dict[str, str]) -> str:
    """渲染 jar 内文本源译文内容（**不写回 jar**，返回渲染后文件内容字符串）。

    供整合包模式把 jar 内 json/lines（教程书/进度）分流到资源包/补丁包——整合包
    不产 hardcoded 修改版 jar，全走资源包 / VP / 补丁形式生效（用户刚需）。
    渲染逻辑与 write_translated 相同（解压 jar → 按 kind 渲染），只返回内容由调用方落盘。
    """
    if not translations:
        return ""
    work = jar.parent / f".{jar.stem}_rs"
    try:
        _extract_jar(jar, work)
        if source.kind == "lines":
            return _render_lines(work, source, translations)
        if source.kind == "json":
            return _render_json(work, source, translations)
        return _render_lang(work, source, translations)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def render_jar_sources_batch(jar: Path, updates: list[tuple[TextSource, dict[str, str]]]) -> list[tuple[str, str]]:
    """批量渲染 jar 内文本源译文内容（**一次解压 jar**，渲染全部 sources，返回
    [(target_path, content)]）。

    修复（recheck，用户实测卡住）：原 render_jar_source 逐个 source 调用时**每次全量解压
    + 删除整个 jar** → O(n²)。大 jar（AdvancedPeripherals 教程书等几十个文本源）卡在
    进度「正在写入 jar 教程/进度文本…(1/N)」长时间不动。这里解压一次，批量渲染后删。"""
    if not updates:
        return []
    work = jar.parent / f".{jar.stem}_rs"
    out: list[tuple[str, str]] = []
    try:
        _extract_jar(jar, work)
        for source, translations in updates:
            if not translations:
                continue
            if source.kind == "lines":
                content = _render_lines(work, source, translations)
            elif source.kind == "json":
                content = _render_json(work, source, translations)
            else:
                content = _render_lang(work, source, translations)
            out.append((source.target_path, content))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out


# ---------- 语言文件写回（modjar 单一汉化 jar 用） ----------

# modid 不可信：白名单 [A-Za-z0-9_-]，不含 "."，杜绝 ".."、"/" 等路径穿越（与资源包一致）。
# 修复：允许大写（jar 内语言文件路径可能用大写 modid，原全小写正则 fullmatch 拒绝导致
# 该 mod 语言文件写回被跳过、产物缺失）
_MODID_RE = re.compile(r"[A-Za-z0-9_-]+")

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


def _read_en_us_keys(work: Path, modid: str, ext: str) -> set[str]:
    """读 jar 内 en_us 语言文件键集（对齐产物用）。en_us 缺失返回空集（调用方跳过过滤）。"""
    p = work / "assets" / modid / "lang" / f"en_us.{ext}"
    if not p.exists():
        return set()
    try:
        return set(_parse_lang_entry(ext, p.read_bytes().decode("utf-8-sig")))
    except Exception:
        return set()


def write_lang_into_jar(jar: Path, by_mod: dict[str, dict[str, str]], target_lang: str,
                        pack_format: int) -> None:
    """把 {modid: {key: 译文}} 语言文件翻译写回 jar（modjar 单一汉化 jar 用）。

    修改发生在 jar 副本上（调用方保证）。流程：安全解压 → 逐 modid 写
    assets/<modid>/lang/<target>.<ext>（目标语言文件已存在则合并已有条目）→ 重打包。
    格式沿用 jar 内已有目标/源语言文件，否则按 pack_format 推断（json/lang）。
    modid 走白名单校验，拦截路径穿越；by_mod 为空则直接返回。

    对齐 en_us 键集（Xaero 审查修复）：合并旧 zh_cn 时删除 en_us 中不存在的键——
    原 jar 自带 zh_cn 可能是旧版本/合并事故残留（报告实测多出 5 个 en_us 没有的键），
    保留会让产物语言文件比 en_us 多键，覆盖其他资源。en_us 键集中的已汉化键保留
    （含 in 跳过的正确中文），entries 新键并入。
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
                data = _parse_lang_entry(ext, target.read_bytes().decode("utf-8-sig"))
            en_us_keys = _read_en_us_keys(work, modid, ext)
            if en_us_keys:
                # 对齐 en_us：删旧残留键（en_us 没有的）；en_us 有但未翻的键保留原值
                data = {k: v for k, v in data.items() if k in en_us_keys}
            data.update(entries)
            # 用字节写入：避开 Path.write_text 在 Windows 上默认把 \n 转 \r\n 的坑
            target.write_bytes(_serialize_lang(ext, data).encode("utf-8"))
        _repack(work, jar)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------- 整合包目录文本源（任务线 / config / data / kubejs） ----------

# 配置开关/布尔字面量：不翻译（true/false/on/off 等），避免把布尔配置翻译坏
_SWITCH_LITERALS = {
    "true", "false", "yes", "no", "on", "off", "null", "none",
    "enabled", "disabled", "enable", "disable",
}


def _pack_should_translate(text: str) -> bool:
    """目录文本源过滤：should_translate + 配置开关字面量跳过（布尔/开关配置不翻译）
    + 动画 easing 枚举跳过（linear/step/bounce 等，翻译破坏动画加载）。
    max_len 放宽到 5000：目录 json 只收 FTBQ/advancements/kubejs lang/patchouli 等明确
    文本载体，长教程正文不被漏提（修复 recheck）。"""
    if not should_translate(text, max_len=5000):
        return False
    t = text.strip().lower()
    if t in _SWITCH_LITERALS or t in _EASING_KEYWORDS:
        return False
    return True


def _pack_modid(rel: str) -> str:
    """目录文本源的 modid 元数据：data/ 下取 namespace，其他取路径第一段。"""
    parts = rel.split("/")
    if parts[0] == "data" and len(parts) > 2:
        return parts[1]
    return parts[0]


def _pack_json_source(root: Path, p: Path, rel: str, target_lang: str = "zh_cn") -> TextSource | None:
    """config/data 下 json：递归字符串值，key_path 定位；技术串/开关值过滤。
    target_path 用 _zh_cn_path（en_us → 目标语言）——修复（v1.2.0）：原用 rel（==source）
    导致整合包目录 json（kubejs/assets/*/lang、advancements）译文写回 en_us 原路径，
    游戏按目标语言读 zh_cn 永远看不到（用户实测 KubeJS 物品 + FTB 任务翻译键未汉化）。"""
    data = json.loads(p.read_bytes().decode("utf-8-sig"))
    entries: dict[str, str] = {}
    # advancements 只收 display.title/description 文本，不收 criteria 触发条件（代码逻辑）
    _walk_json(data, "", entries, _pack_should_translate,
               allow_keys=_ADV_TEXT_PATHS if _is_advancement_json(rel) else None)
    if not entries:
        return None
    return TextSource(kind="json", modid=_pack_modid(rel),
                      source_path=rel, target_path=_zh_cn_path(rel, target_lang), entries=entries)


# 转义辅助：snbt/js 字符串字面量处理（snbt 提取反转义 + render 写回转义）。
_JS_UNESCAPE_MAP = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "'": "'"}


def _js_unescape(body: str) -> str:
    """字符串字面量反转义（常见转义序列），未知转义保留原样。"""
    def _rep(m):
        return _JS_UNESCAPE_MAP.get(m.group(1), m.group(1))
    return re.sub(r"\\(.)", _rep, body)


def _js_escape(text: str, quote: str) -> str:
    """按引号类型转义译文，保证写入字符串字面量语法正确。"""
    return (text.replace("\\", "\\\\").replace(quote, "\\" + quote)
            .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))


# snbt 任务书（FTB Quests / Better Questing）：字段值引号字符串 + description 数组元素
# FTB Quests 任务书键（兼容旧/新格式）：
#   - 旧格式（1.20 前）：title:"文本"（章节文件内嵌）
#   - 新格式（FTB Quests 2100.1.0，1.20+）：quest.<id>.title:"文本" / quest.<id>.quest_desc:["a","b"]
#     / chapter.<id>.title（文本在 config/ftbquests/quests/lang/en_us.snbt，章节文件是翻译键占位符）
# 描述键 1.20+ 是 quest_desc（非 description）——旧正则漏翻描述（用户实测任务书没翻译）
_SNBT_KEY_STR_RE = re.compile(
    r'(?:(?:quest|chapter|chapter_group)\.[A-Za-z0-9_-]+\.)?'
    r'(?:title|name|subtitle|text|quest_desc|quest_subtitle|chapter_subtitle|description)'
    r'\s*:\s*"((?:\\.|[^"\\])*)"')
# description 数组头（值用 _snbt_array_body 引号感知扫描，支持跨行 + 元素内 ] 不截断）
_SNBT_DESC_RE = re.compile(
    r'(?:(?:quest|chapter|chapter_group)\.[A-Za-z0-9_-]+\.)?'
    r'(?:quest_desc|chapter_subtitle|description|subtitle)'
    r'\s*:\s*\[')


def _snbt_array_body(raw: str, start: int) -> int:
    """从数组开 `[` 位置扫描到**引号外**的 `]`，返回其下标；无匹配返回 -1。

    跳过字符串字面量内的 `]`（如 "Press [USE]"）——否则 `[^\\]]*` 会在元素内 ] 提前截断，
    后续元素漏提取（修复 recheck：跨行数组 + 元素含 ] 双场景）。
    """
    i = start + 1
    in_str = False
    while i < len(raw):
        c = raw[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == ']':
                return i
        i += 1
    return -1


# ---------- KubeJS 脚本（kubejs/**/*.js）安全提取 ----------

# JS 字符串字面量：单/双引号，含转义（\' \" \\ 等）；反引号模板字符串（含 ${} 插值=代码）不收
_JS_STR_RE = re.compile(r"""(['"])((?:\\.|(?!\1).)*)\1""")
# 文本字段名白名单：这些字段的值是用户可见文本（GUI 文案/tooltip/任务标题/物品名）。
# 比较前先归一化（_js_norm_field）：KubeJS API 是驼峰（displayName），配置文件常为
# 下划线/短横线（display_name、display-name），归一化后统一命中白名单（修复 recheck：
# displayName 漏出白名单 → KubeJS 物品显示名整个脚本不进翻译流程，用户实测未汉化）。
_JS_TEXT_FIELDS = {"text", "title", "name", "subtitle", "description",
                   "tooltip", "line", "lines", "message", "display_name"}
# 数组字段（tooltip: ['a','b'] / lines: [...]）：进入数组后本行及后续行的字符串都收，直到 ]
_ARRAY_FIELDS = {"tooltip", "lines", "text", "line", "description", "message"}
# 字段名提取：`text: 'x'`（字段名+冒号）或 `.title('x')` / `text('x')`（方法调用）
_JS_FIELD_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*:\s*$")
_JS_METHOD_FIELD_RE = re.compile(r"[.(\s]([A-Za-z_$][\w$]*)\s*\(\s*$")


def _js_field_before(before: str) -> str | None:
    """提取字符串字面量前紧邻的字段名（字段名: / 字段名: [ 数组 / .字段名( ）。无则 None。

    数组字段（tooltip: ['a', 'b']）字符串前是 `[`——先剥掉末尾 [ 与空白再匹配字段名。
    """
    stripped = before.rstrip(" \t[")
    m = _JS_FIELD_RE.search(stripped)
    if m:
        return m.group(1)
    m = _JS_METHOD_FIELD_RE.search(stripped)
    if m:
        return m.group(1)
    return None


def _js_norm_field(field: str) -> str:
    """KubeJS 字段名归一化：驼峰/短横线/空格 → 下划线小写（displayName → display_name、
    DisplayName → display_name、display-name → display_name），统一命中白名单。"""
    s = re.sub(r"[-\s]", "_", field)
    s = re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()
    return s


def _js_value_ok(text: str) -> bool:
    """KubeJS 字符串值是否可翻译：排除路径/命令（含/）、modid:item（冒号无空格）、
    代码标识符/纯数字/开关（should_translate 兜底）。"""
    if "/" in text or "\\" in text:
        return False
    if ":" in text and " " not in text:
        return False
    if text.strip().lower() in _SWITCH_LITERALS or text.strip().lower() in _EASING_KEYWORDS:
        return False
    return should_translate(text, max_len=5000)


def _pack_js_source(root: Path, p: Path, rel: str) -> TextSource | None:
    """kubejs/**/*.js 脚本：提取**明确文本字段**的字符串字面量（text/title/name/tooltip/
    line/description/message 及其数组元素），行内偏移替换（保留脚本结构）。

    KubeJS 脚本内联可见文本（GUI 文案/tooltip/任务标题）不走资源包，只能改脚本本身。
    安全提取：只收「字段名: '...'」/「.字段名('...')」及数组字段元素；路径/命令/
    modid:item/标识符等代码串跳过（_js_value_ok），不碰脚本逻辑（id/事件名/注册名）。
    数组字段跨行（tooltip: [\\n 'a',\\n 'b'\\n ]）用 in_array 状态机收集。
    """
    raw = p.read_bytes().decode("utf-8-sig")
    lines, snapshot = _split_lines(raw)
    entries: dict[str, str] = {}
    spans: dict[str, tuple[int, int, int, str]] = {}
    idx = 0
    in_array = False
    for li, line in enumerate(lines):
        opens = line.count("[")
        closes = line.count("]")
        # 数组头检测（修复 recheck）：`tooltip: [` 单独成行（Prettier 常用折行）时本行
        # 没有字符串字面量，原状态机只在「字符串匹配行」置位 in_array → 首元素及后续
        # 元素全漏收（整段 tooltip 漏翻）。这里对每行先扫数组字段头（.tooltip([ /
        # tooltip: [），命中即进入数组状态；同行数组（['a','b']）opens==closes 不提前置位，
        # 仍走字符串匹配逻辑，互不冲突。
        if not in_array and opens > closes:
            for _f in _ARRAY_FIELDS:
                if re.search(rf"(?:\.{re.escape(_f)}\s*\(|{re.escape(_f)}\s*:)\s*\[", line):
                    in_array = True
                    break
        pos = 0
        for m in _JS_STR_RE.finditer(line):
            q = m.group(1)
            content = _js_unescape(m.group(2))
            before = line[pos:m.start()]
            field = _js_field_before(before)
            norm_field = _js_norm_field(field) if field else None
            if in_array or (norm_field and norm_field in _JS_TEXT_FIELDS):
                if _js_value_ok(content):
                    key = f"k{idx}"
                    entries[key] = content
                    # span 指字面量**内容**（去引号）：开引号后 到 内容结束（group(2) 边界）
                    spans[key] = (li, m.end(1), m.end(2), q)
                idx += 1
            # 数组上下文：字段是数组字段且字段名与字符串之间出现 [（同数组首元素）
            if norm_field in _ARRAY_FIELDS and "[" in before:
                in_array = True
            pos = m.end(1)
        if in_array and closes >= opens:
            in_array = False
    if not entries:
        return None
    return TextSource(kind="lines", modid="kubejs", source_path=rel, target_path=rel,
                      entries=entries, line_snapshot=snapshot, spans=spans)


def _pack_snbt_source(root: Path, p: Path, rel: str) -> TextSource | None:
    """config/data 下 .snbt（FTB Quests / Better Questing 任务书）：提取字段值引号字符串
    （title/name/subtitle/text + description 数组元素），行内偏移替换（保留 snbt 结构）。

    任务书是整合包剧情主线，可见文本在任务标题/描述/提示里。只提取这些字段的字符串值，
    不碰 id/icon/坐标等结构化字段（_pack_should_translate 过滤技术串/开关值）。
    """
    raw = p.read_bytes().decode("utf-8")
    lines, snapshot = _split_lines(raw)
    entries: dict[str, str] = {}
    spans: dict[str, tuple[int, int, int, str]] = {}
    idx = 0
    for li, line in enumerate(lines):
        # 键值字段：title:"..." quest.<id>.title:"..." quest.<id>.quest_desc:"..."（值 group(1)）
        for m in _SNBT_KEY_STR_RE.finditer(line):
            content = _js_unescape(m.group(1))
            if _pack_should_translate(content):
                key = f"s{idx}"
                entries[key] = content
                spans[key] = (li, m.start(1), m.end(1), '"')
            idx += 1
    # description 数组元素：description:["a","b"] —— **全文匹配 + 引号感知边界**（修复 recheck：
    # FTB 的 description 是**跨行多元素数组**（`quest_desc: [\n "a",\n "b"\n]`），原逐行 search
    # 匹配不到跨行数组 → 整段描述漏提取不翻译（用户实测「阅读我的描述」长段没翻译）；且元素
    # 内 ]（"Press [USE]"）不再截断数组）
    for dm in _SNBT_DESC_RE.finditer(raw):
        body_start_abs = dm.end()            # `[` 后一位
        close = _snbt_array_body(raw, body_start_abs - 1)
        if close < 0:
            continue
        body = raw[body_start_abs:close]
        for em in re.finditer(r'"((?:\\.|[^"\\])*)"', body):
            # 元素在 raw 中的绝对偏移 → 映射到 (行号, 行内列)，行内替换保留 snbt 结构
            abs_start = body_start_abs + em.start(1)
            li = raw[:abs_start].count("\n")
            line_start = raw.rfind("\n", 0, abs_start) + 1
            col_start = abs_start - line_start
            col_end = col_start + (em.end(1) - em.start(1))   # 用原文长度（含转义），非 unescape 后
            content = _js_unescape(em.group(1))
            if _pack_should_translate(content):
                key = f"s{idx}"
                entries[key] = content
                spans[key] = (li, col_start, col_end, '"')
            idx += 1
    if not entries:
        return None
    return TextSource(kind="lines", modid=_pack_modid(rel), source_path=rel, target_path=rel,
                      entries=entries, line_snapshot=snapshot, spans=spans)


def discover_pack_text_sources(pack_dir: Path, target_lang: str = "zh_cn") -> list[TextSource]:
    """发现整合包目录（非 jar）文本源，复用 TextSource，source_path=整合包相对路径。

    回归 Minecraft 汉化标准（用户确认）：**只翻明确文本载体**——
      - FTB Quests / Better Questing 任务书（config/ftbquests、config/betterquesting）
      - advancements 进度（data/*/advancements/）
      - kubejs 补语言文件（kubejs/assets/*/lang/）与 patchouli 教程书
      - kubejs/**/*.js 脚本的**明确文本字段**（text/title/tooltip 等，安全提取不碰代码逻辑）
    其余 config/data 的 json/toml/properties 是配置参数（开关/坐标/路径/数值），
    翻译必坏且是大整合包十几万条噪音的根源，一律不翻；kubejs 脚本的代码逻辑串
    （事件名/注册名/路径/命令）也不翻（_pack_js_source 字段白名单 + 技术串过滤）。
    技术串/开关/easing 值过滤（_pack_should_translate）。只读不写，原整合包不被改动；
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
            if suffix == ".json" and _is_pack_text_carrier(rel):
                src = _pack_json_source(root, p, rel, target_lang)
            elif suffix == ".snbt" and rel.startswith(("config/ftbquests/", "data/ftbquests/")):
                # FTB Quests / Better Questing 任务书（剧情主线）
                src = _pack_snbt_source(root, p, rel)
            elif suffix == ".js" and rel.startswith("kubejs/") \
                    and not rel.startswith("kubejs/assets/"):
                # KubeJS 脚本内联可见文本（text/title/tooltip 字段，安全提取不碰代码逻辑）
                src = _pack_js_source(root, p, rel)
            else:
                src = None
            if src:
                result.append(src)
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError, OSError, RecursionError):
            continue   # 单个文件坏编码/坏 json/深嵌套递归超限：跳过该文件，不中断整包
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
        data = json.loads(raw.lstrip("﻿"))   # json 剥 BOM（json.loads 遇 BOM 崩）；lines 保留快照
        for key, value in translations.items():
            try:
                _set_path(data, key, value)
            except (KeyError, IndexError, TypeError):
                continue
        return json.dumps(data, ensure_ascii=False, indent=2)
    lines, snapshot = _split_lines(raw)
    # 同一行的多个替换从右到左执行——若从左到右，前一个替换改变行长度会使后续 span
    # 偏移错位（实测 FTB Quests description 数组同行多元素漏尾）
    by_line: dict[int, list[tuple[int, int, str]]] = {}
    for key, value in translations.items():
        span = (source.spans or {}).get(key)
        if span:
            li, start, end = span[0], span[1], span[2]
            if len(span) >= 4 and span[3]:
                value = _js_escape(value, span[3])   # js 字符串字面量：译文转义
            if 0 <= li < len(lines) and 0 <= start <= end <= len(lines[li]):
                by_line.setdefault(li, []).append((start, end, value))
        else:
            idx = _line_index(key)
            if idx is not None and 0 <= idx < len(lines):
                lines[idx] = value
    for li, repls in by_line.items():
        for start, end, value in sorted(repls, key=lambda r: -r[0]):   # 从右到左
            line = lines[li]
            lines[li] = line[:start] + value + line[end:]
    body = snapshot["eol"].join(lines)
    return ("\ufeff" if snapshot["bom"] else "") + body + (snapshot["eol"] if snapshot["trailing_newline"] else "")
