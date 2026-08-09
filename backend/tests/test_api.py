# FastAPI 路由集成测试（任务 13）：config 往返 / 扫描缺口统计 / 目录浏览 / 后台任务调度
import asyncio
import time

import pytest
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
    monkeypatch.setattr(main, "OUTPUTS_DIR", tmp_path / "outputs")
    monkeypatch.setattr(main, "STORE", TaskStore(tmp_path / "tasks"))

    calls = []

    async def fake_run(task_id, req, cfg, store, work_dir, outputs_dir):
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


def test_cancel_pause_flags(tmp_path, monkeypatch):
    # F8：cancel/pause 端点应立即生效——依赖 F1 内存缓存，端点与后台任务共享同一 TaskState 对象
    import app.main as main
    monkeypatch.setattr(main, "WORK_DIR", tmp_path)
    monkeypatch.setattr(main, "OUTPUTS_DIR", tmp_path / "outputs")
    store = TaskStore(tmp_path / "tasks")
    monkeypatch.setattr(main, "STORE", store)

    async def fake_run(task_id, req, cfg, s, work_dir, outputs_dir):
        await asyncio.sleep(0)   # 立即完成，不真正调引擎

    monkeypatch.setattr(main, "run_translation", fake_run)
    r = client.post("/api/translate", json={"path": str(tmp_path)})
    assert r.status_code == 200
    task_id = r.json()["task_id"]

    r = client.post(f"/api/task/{task_id}/cancel")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert store.load(task_id).cancelled is True

    r = client.post(f"/api/task/{task_id}/pause")
    assert r.status_code == 200 and r.json()["paused"] is True
    assert store.load(task_id).paused is True

    # 再次 pause 应回切为 False
    r = client.post(f"/api/task/{task_id}/pause")
    assert r.status_code == 200 and r.json()["paused"] is False


def test_map_scan_invalid_world(tmp_path):
    # 不存在的世界目录 → 400（validate_world 拒绝）
    r = client.post("/api/map-scan", json={"path": str(tmp_path / "nope")})
    assert r.status_code == 400


def test_map_scan_valid_world(tmp_path):
    # 合法世界（含 level.dat + Command 文本）→ 200，至少命中 1 个词条
    from nbtlib import File, Compound, String
    w = tmp_path / "world"; w.mkdir()
    File({"Data": Compound({"Command": String("say Hello world")})}).save(w / "level.dat", gzipped=True)
    r = client.post("/api/map-scan", json={"path": str(w)})
    assert r.status_code == 200 and r.json()["entries"] >= 1


def test_map_scan_counts_mca_not_skipped(tmp_path):
    """M4-mca：.mca 命令方块词条计入 entries，mca_skipped 恒 0（不再跳过）。"""
    import io
    import struct
    import zlib
    from nbtlib import Compound, File, Int, List, String
    w = tmp_path / "world"
    (w / "region").mkdir(parents=True)
    File({"Data": Compound({"LevelName": String("t")})}).save(w / "level.dat", gzipped=True)
    root = Compound({
        "DataVersion": Int(3465),
        "block_entities": List[Compound]([Compound({
            "id": String("minecraft:command_block"),
            "Command": String("say mca block"),
            "x": Int(0), "y": Int(60), "z": Int(0),
        })]),
    })
    f = File(root)
    buf = io.BytesIO()
    File.write(f, buf)
    payload = zlib.compress(buf.getvalue())
    unit = struct.pack(">I", len(payload) + 1) + b"\x02" + payload
    offsets = bytearray(4096)
    offsets[0:4] = struct.pack(">I", (2 << 8) | 1)
    (w / "region" / "r.0.0.mca").write_bytes(
        bytes(offsets) + bytes(4096) + unit + b"\x00" * (4096 - len(unit)) + bytes(4096))
    r = client.post("/api/map-scan", json={"path": str(w)})
    assert r.status_code == 200
    body = r.json()
    assert body["mca_skipped"] == 0
    assert any(e["nbt_path"].startswith("chunk(0,0)") for e in body["preview"])


def test_hardcode_scan_invalid(tmp_path):
    # 非 .jar 文件 → 400（原 jar 只读，接口拒绝非 jar 输入）
    r = client.post("/api/hardcode-scan", json={"path": str(tmp_path / "notajar.txt")})
    assert r.status_code == 400


def test_hardcode_scan_valid(tmp_path):
    # 复用 test_hardcode 的造 jar 逻辑（javac 编译；无 javac 时造一个含 class 的空 zip 即可测 200）
    import shutil, subprocess, zipfile
    if shutil.which("javac"):
        srcdir = tmp_path / "s"; srcdir.mkdir()
        (srcdir / "HelloMod.java").write_text(
            'public class HelloMod { public static void main(String[] a) { System.out.println("Hello World"); } }',
            encoding="utf-8")
        classes = tmp_path / "c"; classes.mkdir()
        subprocess.run(["javac", "-d", str(classes), str(srcdir / "HelloMod.java")], check=True)
        jar = tmp_path / "mod.jar"
        with zipfile.ZipFile(jar, "w") as zf:
            for f in classes.rglob("*.class"):
                zf.write(f, f.relative_to(classes).as_posix())
        r = client.post("/api/hardcode-scan", json={"path": str(jar)})
        assert r.status_code == 200 and r.json()["count"] >= 1 and "Hello World" in r.json()["strings"]
    else:
        pytest.skip("无 javac")


def test_hardcode_translate_returns_task(tmp_path):
    # 端点应返回 {task_id}，不等待后台跑完（参照地图 translate 测试模式）
    jar = tmp_path / "mod.jar"
    jar.write_bytes(b"PK\x05\x06\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")  # 空 zip
    r = client.post("/api/hardcode-translate", json={"path": str(jar)})
    assert r.status_code == 200 and "task_id" in r.json()


def test_hardcode_scan_bad_zip(tmp_path):
    # 假 jar（不是有效 zip）→ 400 "不是有效的 jar/zip 文件"，而不是 500
    fake = tmp_path / "fake.jar"
    fake.write_bytes(b"this is definitely not a zip archive")
    r = client.post("/api/hardcode-scan", json={"path": str(fake)})
    assert r.status_code == 400
    assert "不是有效的" in r.json()["detail"]


def test_hardcode_translate_invalid(tmp_path):
    # 非 .jar 文件 → 400（与 scan 端点对称校验，避免误选目录白白启动必失败任务）
    r = client.post("/api/hardcode-translate", json={"path": str(tmp_path / "notajar.txt")})
    assert r.status_code == 400


# —— 任务 O1：/api/test-connection 连接检测 ——
def test_test_connection_llm_bad_key(tmp_path, monkeypatch):
    # LLM 分支：httpx.post 模拟 401 → ok=False，message 不含 api_key
    import app.main as main
    import types
    (tmp_path / "config.json").write_text(
        '{"engine": "llm", "provider": "DeepSeek", "llm": {"base_url": "https://x", "model": "m"}}',
        encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(main, "_read_api_key", lambda cfg: "sk-secret")
    monkeypatch.setattr(main.httpx, "post",
                        lambda *a, **k: types.SimpleNamespace(status_code=401))
    r = client.post("/api/test-connection", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "API Key 无效" in body["message"]
    assert "sk-secret" not in str(body)


def test_test_connection_llm_ok(tmp_path, monkeypatch):
    # LLM 分支：httpx.post 模拟 200 → ok=True，响应不含 api_key
    import app.main as main
    import types
    (tmp_path / "config.json").write_text(
        '{"engine": "llm", "provider": "DeepSeek", "llm": {"base_url": "https://x", "model": "m"}}',
        encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(main, "_read_api_key", lambda cfg: "sk-secret")
    monkeypatch.setattr(main.httpx, "post",
                        lambda *a, **k: types.SimpleNamespace(status_code=200))
    r = client.post("/api/test-connection", json={})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "sk-secret" not in str(r.json())


def test_test_connection_machine_down(tmp_path, monkeypatch):
    # 机翻分支：deep_translator 抛异常 → ok=False，提示机翻服务不可用（不真发外网）
    import app.main as main
    (tmp_path / "config.json").write_text(
        '{"engine": "machine", "machine": {"provider": "google"}}', encoding="utf-8")
    monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "config.json")
    # 防御：machine 分支不应触发任何 httpx 请求
    monkeypatch.setattr(main.httpx, "post",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应发请求")))

    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr("deep_translator.GoogleTranslator", boom)
    r = client.post("/api/test-connection", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and "机翻服务不可用" in body["message"]


# —— 缓存清理 + 占用统计（任务：缓存清理与配置持久化开屏修复）——
def test_cache_size_empty(tmp_path, monkeypatch):
    # 空目录（WORK_DIR/OUTPUTS_DIR 均无文件）→ total_mb=0，路径返回
    import app.main as main
    work = tmp_path / "work"; work.mkdir()
    out = tmp_path / "outputs"; out.mkdir()
    monkeypatch.setattr(main, "WORK_DIR", work)
    monkeypatch.setattr(main, "OUTPUTS_DIR", out)
    r = client.get("/api/cache-size")
    assert r.status_code == 200
    body = r.json()
    assert body["total_mb"] == 0
    assert body["work_bytes"] == 0 and body["outputs_bytes"] == 0
    assert body["work_path"] == str(work) and body["outputs_path"] == str(out)


def test_cache_size_counts(tmp_path, monkeypatch):
    # 目录里放文件（含嵌套子目录）→ work/outputs 字节数分别统计，total_mb 保留 1 位小数
    import app.main as main
    work = tmp_path / "work"; work.mkdir()
    out = tmp_path / "outputs"; out.mkdir()
    (work / "a.bin").write_bytes(b"x" * (1024 * 1024))          # 1 MB
    (out / "sub").mkdir()
    (out / "sub" / "b.bin").write_bytes(b"y" * (512 * 1024))    # 0.5 MB
    monkeypatch.setattr(main, "WORK_DIR", work)
    monkeypatch.setattr(main, "OUTPUTS_DIR", out)
    r = client.get("/api/cache-size")
    assert r.status_code == 200
    body = r.json()
    assert body["work_bytes"] == 1024 * 1024
    assert body["outputs_bytes"] == 512 * 1024
    assert body["total_mb"] == 1.5


def test_clear_cache_removes(tmp_path, monkeypatch):
    # 放文件 → clear_cache → 两目录重建且空，cleared_bytes/cleared_mb 正确
    import app.main as main
    work = tmp_path / "work"; work.mkdir()
    out = tmp_path / "outputs"; out.mkdir()
    (work / "a.bin").write_bytes(b"x" * (1024 * 1024))
    (out / "b.bin").write_bytes(b"y" * (1024 * 1024))
    monkeypatch.setattr(main, "WORK_DIR", work)
    monkeypatch.setattr(main, "OUTPUTS_DIR", out)
    r = client.post("/api/clear-cache")
    assert r.status_code == 200
    body = r.json()
    assert body["cleared_bytes"] == 2 * 1024 * 1024
    assert body["cleared_mb"] == 2.0
    # 目录重建且空（TaskStore 等依赖 WORK_DIR 存在）
    assert work.is_dir() and list(work.iterdir()) == []
    assert out.is_dir() and list(out.iterdir()) == []


def test_config_marks_configured(tmp_path, monkeypatch):
    # 保存配置（engine 等）→ GET /api/config 返回含 engine（前端据此不弹开屏设置）
    import app.main as main
    monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "config.json")
    r = client.post("/api/config", json={"engine": "llm"})
    assert r.status_code == 200
    r = client.get("/api/config")
    assert r.status_code == 200 and r.json()["engine"] == "llm"


def test_config_configured_flag(tmp_path, monkeypatch):
    # 开屏判断依据：未保存过配置 → GET 无 configured；保存后 → configured=True
    import app.main as main
    monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "config.json")
    r = client.get("/api/config")
    assert r.status_code == 200 and "configured" not in r.json()
    r = client.post("/api/config", json={"engine": "llm", "target_lang": "zh_tw"})
    assert r.status_code == 200 and r.json().get("configured") is True
    r = client.get("/api/config")
    assert r.status_code == 200 and r.json().get("configured") is True
