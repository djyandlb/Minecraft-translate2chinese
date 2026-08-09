import json
import threading
from pathlib import Path

# 多任务共享同一记忆文件时，用锁防读/写并发覆盖（F4）
_LOCK = threading.Lock()


class MemoryStore:
    """翻译记忆：{(lang, 原文): 译文} 持久化。翻译前先查记忆，命中直接填，miss 才调引擎。
    key 复合目标语言，避免跨语言污染（zh_cn 记忆误命中 zh_tw、set 互相覆盖）（F3）。"""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, str] = {}
        if path.exists():
            with _LOCK:
                self.data = json.loads(path.read_text(encoding="utf-8"))

    def _key(self, source: str, lang: str) -> str:
        return f"{lang}\x00{source}"

    def get(self, source: str, lang: str) -> str | None:
        return self.data.get(self._key(source, lang))

    def set(self, source: str, lang: str, translated: str) -> None:
        self.data[self._key(source, lang)] = translated

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
