import json
import re
import threading
from pathlib import Path

from app.placeholder import clean_surrogates

# 多任务共享同一记忆文件时，用锁防读/写并发覆盖（F4）
_LOCK = threading.Lock()

# 术语提取：句号/问号/感叹号/分号等句子标点 → 判定为句子不是术语（跳过）
_TERM_SENT_RE = re.compile(r"[.!?。！？；;]")


def extract_terms(data: dict, lang: str, max_terms: int = 150) -> dict[str, str]:
    """从翻译记忆提取「短术语对照」（英文专有名词/物品名 → 已确认译名）。

    术语统一（用户诉求）：同一专有名词全篇必须一个译名，不能乱。把记忆里已确认的
    短词条（2-24 字符、无句子标点）作为对照表注入 prompt，让 AI 翻译时沿用——
    只取短词（长句/整段不是术语），最多 max_terms 条防 prompt 过长。
    """
    prefix = f"{lang}\x00"
    terms: dict[str, str] = {}
    for k, trans in data.items():
        if not k.startswith(prefix) or not trans:
            continue
        src = k[len(prefix):]
        if not (2 <= len(src) <= 24 and len(trans) <= 24):
            continue
        if _TERM_SENT_RE.search(src):
            continue   # 句子不是术语
        terms[src] = trans
    # 修复：按「短术语优先」排序取前 max_terms（专有名词/物品名术语统一价值更高），
    # 而非按记忆插入序截断（插入序会让先写入的冷门长句挤掉高频常用术语）
    return dict(sorted(terms.items(), key=lambda kv: len(kv[0]))[:max_terms])


class MemoryStore:
    """翻译记忆：{(lang, 原文): 译文} 持久化。翻译前先查记忆，命中直接填，miss 才调引擎。
    key 复合目标语言，避免跨语言污染（zh_cn 记忆误命中 zh_tw、set 互相覆盖）（F3）。"""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, str] = {}
        if path.exists():
            with _LOCK:
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, ValueError, OSError):
                    loaded = {}   # 修复：损坏记忆文件回退空表，不崩
                self.data = loaded if isinstance(loaded, dict) else {}

    def _key(self, source: str, lang: str) -> str:
        return f"{lang}\x00{source}"

    def get(self, source: str, lang: str) -> str | None:
        return self.data.get(self._key(source, lang))

    def set(self, source: str, lang: str, translated: str) -> None:
        # 修复：写记忆前清理无效 surrogate（utf-8 写盘 "surrogates not allowed" 崩溃根因兜底；
        # 引擎输出源头已清，此处双保险防其他来源）
        self.data[self._key(source, lang)] = clean_surrogates(translated)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            # 修复：原子写（临时文件 + os.replace），写中断不损坏记忆文件
            import os
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
