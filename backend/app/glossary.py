import json
import re
from pathlib import Path


def strip_particle(trans: str) -> str:
    """剥离中文译名**结尾**的格助词（符文的→符文、宝珠的→宝珠、狂乱的→狂乱）。

    系统性修复「强化符文的的宝珠」/「基于的附魔的过滤器」这类助词冗余：词级术语译名
    若以「的/地/得/之/了」结尾（AI 给的语境化译名，只适合完整句子），作词级术语注入
    会让 AI 在「X of the Y」结构里再叠一个助词。只剥**结尾**：多词短语
    （宝珠的符文 结尾是名词「文」）不受影响；连续助词结尾（「的的」）全剥；
    剥空/剩单字（「刃的」→「刃」）则返回原样，保护有意义译名。
    """
    if not trans:
        return trans
    s = trans.rstrip("的地得之了")
    if not s or len(s) < 2:
        return trans          # 剥离过度（只剩空/单字）→ 保留原样
    return s


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
    """把术语表拼进 AI 提示词，最多取前 limit 条；空表返回空串。
    译名统一过 strip_particle（剥结尾格助词）——所有术语来源（用户表/记忆提取/
    动态登记）注入前都净化，杜绝「符文的」这类语境化译名污染 AI。"""
    items = list(glossary.items())[:limit]
    if not items:
        return ""
    lines = [f"{k} => {strip_particle(v)}" for k, v in items]
    return "术语表（翻译必须遵守）：\n" + "\n".join(lines)
