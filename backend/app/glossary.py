import json
from pathlib import Path

def load_glossary(path: Path) -> dict[str, str]:
    """加载术语表 json（{原文: 译文}）；文件不存在返回空表。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError, OSError):
        return {}   # 修复：损坏术语表回退空表，不崩
    return data if isinstance(data, dict) else {}

def term_inject_prompt(glossary: dict[str, str], limit: int = 30) -> str:
    """把术语表拼进 AI 提示词，最多取前 limit 条；空表返回空串。"""
    items = list(glossary.items())[:limit]
    if not items:
        return ""
    lines = [f"{k} => {v}" for k, v in items]
    return "术语表（翻译必须遵守）：\n" + "\n".join(lines)
