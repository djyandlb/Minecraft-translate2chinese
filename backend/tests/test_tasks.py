from pathlib import Path
from app.tasks import TaskStore


def test_new_and_persist(tmp_path: Path):
    # 新建任务 → 改状态 → 保存 → 重新加载，验证 V3 字段（tokens/paused）落盘
    store = TaskStore(tmp_path / "tasks")
    t = store.new()
    t.status = "running"
    t.total = 10
    t.tokens_in = 100
    t.paused = True
    store.save(t)
    loaded = store.load(t.id)
    assert loaded is not None
    assert loaded.status == "running" and loaded.total == 10
    assert loaded.tokens_in == 100 and loaded.paused is True


def test_load_missing_returns_none(tmp_path: Path):
    # 不存在的任务 id 应返回 None
    store = TaskStore(tmp_path / "tasks")
    assert store.load("nope") is None


def test_list_returns_saved(tmp_path: Path):
    # 两个任务应都能被 list 枚举出来
    store = TaskStore(tmp_path / "tasks")
    store.new()
    store.new()
    assert len(store.list()) == 2
