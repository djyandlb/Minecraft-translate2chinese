import json
import re
from pathlib import Path

# 去 // 与 /* */ 注释（部分 mod 语言文件含注释）。已知局限：字符串值内的 "//"（如 URL）会被截断，社区同类工具一致，可接受。
_COMMENT_RE = re.compile(r"//.*?$|/\*.*?\*/", re.MULTILINE | re.DOTALL)

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
