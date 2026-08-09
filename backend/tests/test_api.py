# FastAPI 路由集成测试（任务 13）：config 往返 / 扫描缺口统计 / 目录浏览 / 后台任务调度
import asyncio
import time

from fastapi.testclient import TestClient

from app.main import app
from app.config import AppConfig
from app.tasks import TaskStore

client = TestClient(app)


def test_config_roundtrip(tmp_path, monkeypatch):
    # 通过 GET /api/config 拿默认，POST 修改后 GET 验证
    import app.main as main
    monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "config.json")
    r = client.get("/api/config")
    assert r.status_code == 200 and r.json()["target_lang"] == "zh_cn"
    r = client.post("/api/config", json={"target_lang": "zh_tw"})
    assert r.status_code == 200 and r.json()["target_lang"] == "zh_tw"
    # api_key 禁止落盘
    client.post("/api/config", json={"api_key": "secret"})
    assert "api_key" not in main.AppConfig(tmp_path / "config.json").data


def test_scan_modpack(tmp_path):
    import zipfile, json
    mods = tmp_path / "mods"; mods.mkdir()
    with zipfile.ZipFile(mods / "m1.jar", "w") as zf:
        zf.writestr("assets/m1/lang/en_us.json", json.dumps({"a": "One", "b": "Two"}))
    r = client.post("/api/scan", json={"path": str(tmp_path), "mode": "modpack"})
    assert r.status_code == 200
    body = r.json()
    assert body["total_gaps"] == 2


def test_browse(tmp_path):
    (tmp_path / "subdir").mkdir()
    r = client.get("/api/browse", params={"path": str(tmp_path)})
    assert r.status_code == 200 and "subdir" in r.json()["dirs"]


def test_translate_starts_background_task(tmp_path, monkeypatch):
    # translate 端点是 async def：create_task 需要 running loop，否则同步端点会 500。
    # 此处隔离 work/task 目录并替换 run_translation，验证后台任务被调度。
    import app.main as main
    monkeypatch.setattr(main, "WORK_DIR", tmp_path)
    monkeypatch.setattr(main, "STORE", TaskStore(tmp_path / "tasks"))

    calls = []

    async def fake_run(task_id, req, cfg, store, work_dir):
        calls.append((task_id, req))
        await asyncio.sleep(0)

    monkeypatch.setattr(main, "run_translation", fake_run)
    r = client.post("/api/translate", json={"path": str(tmp_path)})
    assert r.status_code == 200
    task_id = r.json()["task_id"]
    for _ in range(50):          # 轮询等后台任务执行（TestClient 同步跑 portal loop）
        if calls:
            break
        time.sleep(0.02)
    assert calls and calls[0][0] == task_id
