# 桌面壳相关测试（M6-1）：前端静态服务、BASE frozen 路径、空闲端口
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app, FRONT_DIST


def test_static_served_when_dist_exists():
    """前端 dist 存在时：/ 返回 index.html，未知 /api 仍 404，SPA fallback 生效。"""
    if not (FRONT_DIST / "index.html").exists():
        import pytest
        pytest.skip("前端 dist 不存在（先 npm run build）")
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    # 未知 api 路径不应被 SPA fallback 吞掉（应 404 而非 html）
    r2 = client.get("/api/no-such-endpoint")
    assert r2.status_code == 404
    # SPA fallback：未知前端路由回 index.html
    r3 = client.get("/some/frontend/route")
    assert r3.status_code == 200


def test_frozen_base_switches_to_executable_dir(monkeypatch):
    """frozen 时 _base() 指向 exe 同目录（可写），非 frozen 指向 backend/。"""
    from app import main as m
    # frozen 路径：exe 同目录
    monkeypatch.setattr(m.sys, "frozen", True, raising=False)
    monkeypatch.setattr(m.sys, "executable", r"C:\fake\exe\dir\app.exe")
    assert m._base() == Path(r"C:\fake\exe\dir")
    # 非 frozen 路径：backend/
    monkeypatch.setattr(m.sys, "frozen", False, raising=False)
    assert m._base() == Path(m.__file__).resolve().parent.parent


def test_front_dist_frozen_points_to_meipass(monkeypatch):
    """frozen 时 _front_dist() 指向 _MEIPASS/frontend/dist，否则项目根 frontend/dist。"""
    from app import main as m
    monkeypatch.setattr(m.sys, "frozen", True, raising=False)
    monkeypatch.setattr(m.sys, "_MEIPASS", r"C:\fake\meipass", raising=False)
    assert m._front_dist() == Path(r"C:\fake\meipass\frontend\dist")
    monkeypatch.setattr(m.sys, "frozen", False, raising=False)
    assert m._front_dist() == m.BASE.parent / "frontend" / "dist"


def test_free_port_returns_open_port():
    """_free_port() 返回一个 127.0.0.1 上确实可绑定的空闲端口。"""
    import socket
    from app.desktop import _free_port
    port = _free_port()
    assert 0 < port < 65536
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))
