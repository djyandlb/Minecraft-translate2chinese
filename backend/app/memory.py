import json
from pathlib import Path


class MemoryStore:
    """翻译记忆：{原文: 译文} 持久化。翻译前先查记忆，命中直接填，miss 才调引擎。"""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, str] = {}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))

    def get(self, source: str) -> str | None:
        return self.data.get(source)

    def set(self, source: str, translated: str) -> None:
        self.data[source] = translated

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
