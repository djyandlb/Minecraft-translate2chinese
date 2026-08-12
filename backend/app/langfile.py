import json
import re
from pathlib import Path

def _strip_comments(text: str) -> str:
    """剥离 JSON 注释（// 与 /* */），但跳过字符串字面量内部的 // 与 /*。

    修复（原正则直接把字符串值内的 "//" 如 URL http://... 当注释截断到行尾，
    导致整文件 JSON 解析失败被跳过）。逐字符扫描维护状态：双引号字符串内
    的 // 与 /* 原样保留，仅剥离字符串外真正的注释。
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_str = in_line = in_block = False
    while i < n:
        c, nxt = text[i], (text[i + 1] if i + 1 < n else "")
        if in_line:
            if c == "\n":
                in_line = False
                out.append(c)
            i += 1
            continue
        if in_block:
            if c == "*" and nxt == "/":
                in_block = False
                i += 2
            else:
                i += 1
            continue
        if in_str:
            out.append(c)
            if c == "\\" and nxt:
                out.append(nxt)
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif c == "/" and nxt == "/":
            in_line = True
            i += 2
        elif c == "/" and nxt == "*":
            in_block = True
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)

# 语言文件值「含实际含义」判定：拉丁字母或汉字（纯数字/纯符号串不含，跳过）
# 含「有实际含义」的字符：拉丁字母、汉字（含扩展 A 区 㐀-䶿）、假名（日文源语言文件纯假名值
# 不被误判「无意义」漏翻——修复：旧正则只有一-鿿，不含平假名/片假名）
_LANG_VALUE_MEANING_RE = re.compile(r"[a-zA-Z一-鿿㐀-䶿ぁ-んァ-ヶ]")


def lang_value_ok(value: str) -> bool:
    """语言文件的值是否值得翻译：仅「长度 2-500 + 含字母/汉字」。

    语言文件的值本就是可翻译文本（键才是标识符），因此不走 should_translate
    的技术标识符规则——"Requires_Armor" 这类 snake_case 形态是真实英文短语，
    不能被当 iron_ingot 那样的技术串滤掉。只排除过短/纯数字/纯符号值。
    长度上限 500（修复：长段工具提示/说明 >200 字符被旧上限误过滤 → 漏翻，
    MC 语言文件说明最长约 400-500 字符）。
    """
    if not (2 <= len(value) <= 500):
        return False
    return _LANG_VALUE_MEANING_RE.search(value) is not None

def parse_json_lang(text: str) -> dict[str, str]:
    """解析 JSON 语言文件，容忍 // 与 /* */ 注释（字符串字面量内的 // 不剥离）。只保留字符串值条目。"""
    cleaned = _strip_comments(text)
    data = json.loads(cleaned)
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}

def parse_lang(text: str) -> dict[str, str]:
    """解析 .lang：每行 key=value，# 开头为注释。"""
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        result[k.strip()] = v.strip()
    return result


def parse_properties(text: str) -> dict[str, str]:
    """解析 Java .properties：每行 key=value（或 key: value），#/! 开头注释与空行跳过。

    Java Properties 格式允许 = 或 : 作键值分隔符，行首 # 与 ! 均视为注释。
    不做转义/续行处理——语言文件实际使用中极罕见，社区同类工具一致，可接受。
    """
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
        elif ":" in line:
            k, v = line.split(":", 1)
        else:
            continue
        result[k.strip()] = v.strip()
    return result

def load_lang_file(path: Path) -> tuple[dict[str, str], str]:
    """读语言文件，返回 (entries, 格式)，格式为 "json" 或 "lang"。"""
    # 修复（recheck）：utf-8-sig 剥 BOM——带 BOM 的 en_us.json 之前 json.loads 抛
    # JSONDecodeError 整文件漏翻；.lang 的 BOM 污染首键 ﻿key 匹配不到
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix == ".json":
        return parse_json_lang(text), "json"
    return parse_lang(text), "lang"

def write_json_lang(data: dict[str, str]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)

def write_lang(data: dict[str, str]) -> str:
    return "\n".join(f"{k}={v}" for k, v in data.items()) + "\n"

def write_properties(data: dict[str, str]) -> str:
    """把条目序列化为 .properties 文本（key=value 每行，末尾换行）。"""
    return "\n".join(f"{k}={v}" for k, v in data.items()) + "\n"
