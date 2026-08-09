import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class TaskState:
    """翻译任务状态，json 持久化。断点续翻 + 暂停/取消 + token 统计（V3）。"""
    id: str
    status: str = "pending"               # pending/running/done/failed/paused
    total: int = 0
    done: int = 0
    failed: int = 0
    paused: bool = False
    cancelled: bool = False
    tokens_in: int = 0
    tokens_out: int = 0
    cost_estimate: float = 0.0
    progress: list[dict] = field(default_factory=list)  # [{key, source, translated, status}]
    created_at: float = field(default_factory=time.time)


class TaskStore:
    """任务状态 json 持久化（每个任务一个文件）。"""

    def __init__(self, dir: Path):
        self.dir = dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.json"

    def new(self) -> TaskState:
        state = TaskState(id=uuid.uuid4().hex[:12])
        self.save(state)
        return state

    def save(self, state: TaskState) -> None:
        self._path(state.id).write_text(json.dumps(asdict(state), ensure_ascii=False), encoding="utf-8")

    def load(self, task_id: str) -> TaskState | None:
        p = self._path(task_id)
        if not p.exists():
            return None
        return TaskState(**json.loads(p.read_text(encoding="utf-8")))

    def list(self) -> list[TaskState]:
        return [self.load(p.stem) for p in self.dir.glob("*.json") if p.stem]
