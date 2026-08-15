import asyncio
import json
import os
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
    stage: str = ""                       # 当前阶段 lang/json/pack/hardcode/build
    stages: list[dict] = field(default_factory=list)    # [{"name","total","done"}] 逐阶段明细
    display_name: str = ""                # 输入名（整合包/mod/地图文件名，去扩展名）
    display_name_translated: str = ""     # 输入名的目标语言翻译（完成后右栏显示中文名）
    created_at: float = field(default_factory=time.time)
    reviewing: bool = False               # 审查状态灯：审查管道活跃时 True（前端红灯「静默审查中」）
    project_id: str = ""                  # 项目指纹（内容指纹 hash）——删除项目时精确取消关联任务


class TaskStore:
    """任务状态 json 持久化（每个任务一个文件）。
    带内存缓存：new/save 更新缓存，load 先查缓存，保证端点与后台任务拿到同一对象（F1）。
    save 时广播变更给 SSE 订阅者（前后端联动更及时——审查：替代前端 1s 轮询）。"""

    def __init__(self, dir: Path):
        self.dir = dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, TaskState] = {}
        # SSE 订阅表：{task_id: {asyncio.Queue, ...}}，save 时广播最新状态
        self._subs: dict[str, set[asyncio.Queue]] = {}
        # 暂停事件表（P1-4）：{task_id: asyncio.Event}，暂停/取消即时唤醒翻译协程，
        # 替代 0.5s 轮询 sleep（事件不持久化，任务终态后 discard 清理）
        self._pause_events: dict[str, asyncio.Event] = {}

    def _path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.json"

    def new(self) -> TaskState:
        state = TaskState(id=uuid.uuid4().hex[:12])
        self.save(state)
        return state

    def save(self, state: TaskState) -> None:
        # P1-6：落盘时截断 progress（保留最近 1000 条），防几千条 dict 全量序列化膨胀。
        # 修复（recheck 内存增长）：同时截断内存副本——大整合包数万条 progress 全量常驻
        # 内存翻倍 OOM 风险；前端只显示最近 100、SSE/轮询读的都是截断后状态，内存保留
        # 全量没有消费者（原「不污染内存 state」设计导致内存随条目数无限增长）
        if isinstance(state.progress, list) and len(state.progress) > 1000:
            state.progress = state.progress[-1000:]
        data = asdict(state)
        # 修复（recheck）：损坏任务 json（progress 显式 null）load 得 progress=None →
        # 截断前判空，否则 save 抛 TypeError 冒泡到 pause/cancel 端点
        data["progress"] = (data["progress"] or [])[-1000:]
        # 原子写（修复：直接 write_text 覆盖中途崩溃/断电 → 任务 json 损坏 → 断点续翻丢失；
        # 先写临时文件再 os.replace，对齐 config.save 的原子写）
        p = self._path(state.id)
        # 修复（recheck）：父目录可能被「缓存目录即时切换」移走/不存在——save 先 mkdir，
        # 否则运行中任务在切换后 write_text 抛 FileNotFoundError 冒泡 → 任务被误标 failed
        #（用户改缓存目录时运行中任务崩溃）
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
        self._cache[state.id] = state
        self._notify(state)

    def load(self, task_id: str) -> TaskState | None:
        if task_id in self._cache:
            return self._cache[task_id]
        p = self._path(task_id)
        if not p.exists():
            return None
        # 修复：损坏/旧版本任务文件容错——JSON 解析失败或字段缺失时返回 None 跳过，
        # 否则 /api/tasks 因单个坏文件整个 500
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError, OSError):
            return None
        if not isinstance(raw, dict) or "id" not in raw:
            return None
        try:
            # 只取 TaskState 已知字段，缺失字段用 dataclass 默认值补全
            state = TaskState(**{k: v for k, v in raw.items() if hasattr(TaskState, k)})
        except (TypeError, ValueError):
            return None
        self._cache[task_id] = state
        return state

    def list(self) -> list[TaskState]:
        return [s for s in (self.load(p.stem) for p in self.dir.glob("*.json") if p.stem)
                if s is not None]

    # ---- 暂停事件（P1-4）：暂停/取消即时唤醒，替代 0.5s 轮询 sleep ----
    def pause_event(self, task_id: str) -> asyncio.Event:
        """取任务的暂停事件（懒创建）。暂停态 clear（wait 阻塞）、继续/取消 set（唤醒）。"""
        ev = self._pause_events.get(task_id)
        if ev is None:
            ev = asyncio.Event()
            self._pause_events[task_id] = ev
        return ev

    def discard_pause_event(self, task_id: str) -> None:
        """任务终态后清理暂停事件（防表泄漏）。"""
        self._pause_events.pop(task_id, None)

    # ---- SSE 订阅：save 时广播状态变更（前后端联动更及时）----
    def subscribe(self, task_id: str) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=50)   # 背压：慢消费者丢旧保新
        self._subs.setdefault(task_id, set()).add(q)
        return q

    def unsubscribe(self, task_id: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(task_id)
        if subs and q in subs:
            subs.discard(q)
            if not subs:
                self._subs.pop(task_id, None)   # 无订阅即清表，防泄漏

    def _notify(self, state: TaskState) -> None:
        """save 后广播。调用方保证在事件循环线程（async 协程 / async def 端点）；
        经 call_soon_threadsafe 投递，避免跨线程直接入队的竞态。"""
        subs = self._subs.get(state.id)
        if not subs:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for q in list(subs):
            loop.call_soon_threadsafe(self._put_latest, q, state)

    def _put_latest(self, q: asyncio.Queue, state: TaskState) -> None:
        if q.full():
            try:
                q.get_nowait()   # 队列满：丢最旧保最新
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(state)
