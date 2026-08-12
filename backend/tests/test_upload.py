# 任务 A4 测试：文件上传端点落盘 + browse 跨盘目录浏览
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.main import app

client = TestClient(app)


@pytest.fixture
def work(tmp_path, monkeypatch):
    """上传落盘目录隔离到 tmp_path，避免污染真实 backend/work。"""
    monkeypatch.setattr(main, "WORK_DIR", tmp_path)
    return tmp_path


# 空 zip 的最小字节（PK\x05\x06 头 + 20 个 \x00，共 24 字节）
_PAYLOAD = b"PK\x05\x06\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"


def test_upload_file_lands_on_disk(work):
    # 构造一个小 zip 上传，断言落盘且返回路径/文件名/大小
    r = client.post("/api/upload", files={
        "file": ("mod.jar", _PAYLOAD, "application/octet-stream")
    })
    assert r.status_code == 200
    data = r.json()
    assert "path" in data and Path(data["path"]).exists()
    assert data["name"] == "mod.jar"
    assert data["size"] == len(_PAYLOAD)


def test_upload_filename_sanitized(work):
    # 恶意文件名只取 basename，不逃逸出 work/uploads（防路径穿越）
    r = client.post("/api/upload", files={
        "file": ("../../evil.jar", _PAYLOAD, "application/octet-stream")
    })
    assert r.status_code == 200
    data = r.json()
    assert "evil.jar" == data["name"]           # 斜杠已剥掉
    assert ".." not in data["path"]


def test_browse_drives_on_windows():
    # 盘根路径应返回盘符列表（含 C:），实现跨盘导航
    if os.name != "nt":
        pytest.skip("仅 Windows 测盘符")
    r = client.get("/api/browse", params={"path": "C:\\"})
    assert r.status_code == 200
    assert any("C:" in d for d in r.json()["dirs"])


def test_browse_normal_dir(tmp_path):
    # 普通目录浏览保持原逻辑：返回子目录名
    (tmp_path / "sub").mkdir()
    r = client.get("/api/browse", params={"path": str(tmp_path)})
    assert r.status_code == 200
    assert "sub" in r.json()["dirs"]


def test_browse_d_drive_shows_content():
    # 🟡-3 跨盘端到端：D 盘存在时 browse("D:\\") 必须返回 200、parent==""、
    # dirs 为 D 盘内的子目录（而非盘符列表），否则用户永远进不去 D/E 盘
    if os.name != "nt":
        pytest.skip("仅 Windows 测盘符跨盘导航")
    if not os.path.exists("D:\\"):
        pytest.skip("D 盘不存在，跳过跨盘端到端")
    r = client.get("/api/browse", params={"path": "D:\\"})
    assert r.status_code == 200
    data = r.json()
    assert data["parent"] == ""                       # 盘根 parent 为空，前端不会回退错位
    assert isinstance(data["dirs"], list)
    # dirs 必须是盘内子目录：Windows 目录名不含冒号，盘符条目才含冒号
    assert not any(":" in d for d in data["dirs"])


def test_browse_garbage_path_not_pollute_parent():
    # 🟡-2 🟡-3：前端旧拼接的垃圾路径 C:\/D:\（规范化成 C:\D:，不存在）
    # 必须返回 {parent:"", dirs:[]}，不得回吐脏 parent 覆写前端 browserPath
    if os.name != "nt":
        pytest.skip("垃圾盘符拼接仅在 Windows 出现")
    r = client.get("/api/browse", params={"path": "C:\\/D:\\"})
    assert r.status_code == 200
    assert r.json() == {"parent": "", "dirs": []}


def test_upload_dotdot_filename_falls_back(work):
    # ⚪-4 上传文件名 ".."：净化后若落在 {..,.} 必须回退 "upload"，
    # 否则 dest_dir/".." 落到 uploads 目录 open("wb") 抛 IsADirectoryError → 500
    r = client.post("/api/upload", files={
        "file": ("..", _PAYLOAD, "application/octet-stream")
    })
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "upload"
    assert ".." not in data["path"]


def test_upload_dot_filename_falls_back(work):
    # ⚪-4 上传文件名 "."：同样回退 "upload"，防落盘到目录自身
    r = client.post("/api/upload", files={
        "file": (".", _PAYLOAD, "application/octet-stream")
    })
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "upload"
    assert Path(data["path"]).name == "upload"
