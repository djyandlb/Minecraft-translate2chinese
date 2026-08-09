import json
import re
from pathlib import Path

# 去 // 与 /* */ 注释（部分 mod 语言文件含注释）。已知局限：字符串值内的 "//"（如 URL）会被截断，社区同类工具一致，可接受。
_COMMENT_RE = re.compile(r"//.*?$|/\*.*?\*/", re.MULTILINE | re.DOTALL)

# 语言文件值「含实际含义」判定：拉丁字母或汉字（纯数字/纯符号串不含，跳过）
_LANG_VALUE_MEANING_RE = re.compile(r"[a-zA-Z一-鿿]")


def lang_value_ok(value: str) -> bool:
    """语言文件的值是否值得翻译：仅「长度 2-200 + 含字母/汉字」。

    语言文件的值本就是可翻译文本（键才是标识符），因此不走 should_translate
    的技术标识符规则——"Requires_Armor" 这类 snake_case 形态是真实英文短语，
    不能被当 iron_ingot 那样的技术串滤掉。只排除过短/纯数字/纯符号值。
    """
    if not (2 <= len(value) <= 200):
        return False
    return _LANG_VALUE_MEANING_RE.search(value) is not None

def parse_json_lang(text: str) -> dict[str, str]:
    """解析 JSON 语言文件，容忍 // 与 /* */ 注释。只保留字符串值条目。"""
    cleaned = _COMMENT_RE.sub("", text)
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
    text = path.read_text(encoding="utf-8")
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
