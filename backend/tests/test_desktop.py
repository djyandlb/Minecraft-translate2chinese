# 任务 O2 测试：/api/key/status 端点（keyring 读取，不返回 key）+ desktop._JsApi 对话框选择逻辑
import sys
import types

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.desktop import _JsApi

client = TestClient(app)


# —— /api/key/status ——
def test_key_status_configured(tmp_path, monkeypatch):
    # keyring 已配置 → {configured: true}，且响应体不泄露 key 本身
    import app.main as main
    monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(main, "_read_api_key", lambda cfg: "sk-secret")
    r = client.get("/api/key/status")
    assert r.status_code == 200
    body = r.json()
    assert body == {"configured": True}
    assert "sk-secret" not in str(body)


def test_key_status_not_configured(tmp_path, monkeypatch):
    # keyring 未配置 → {configured: false}
    import app.main as main
    monkeypatch.setattr(main, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(main, "_read_api_key", lambda cfg: "")
    r = client.get("/api/key/status")
    assert r.status_code == 200 and r.json() == {"configured": False}


# —— desktop._JsApi.select_path（不弹真实对话框，注入 fake webview）——
def _fake_webview(return_value):
    """构造 pywebview 假模块：windows[0].create_file_dialog 返回固定值。"""
    class FakeWin:
        def __init__(self):
            self.calls = []

        def create_file_dialog(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return return_value

    win = FakeWin()
    mod = types.SimpleNamespace(windows=[win], FOLDER_DIALOG=1, OPEN_DIALOG=2)
    return mod, win


def test_jsapi_select_folder(monkeypatch):
    # kind='folder'：走 FOLDER_DIALOG，返回选中的目录路径列表
    mod, win = _fake_webview([r"C:\mods"])
    monkeypatch.setitem(sys.modules, "webview", mod)
    res = _JsApi().select_path("folder")
    assert res == [r"C:\mods"]
    assert win.calls and win.calls[0][0] == (1,)   # webview.FOLDER_DIALOG


def test_jsapi_select_file(monkeypatch):
    # kind='file'：走 OPEN_DIALOG，带文件类型过滤，返回路径列表
    mod, win = _fake_webview([r"C:\mods\m.jar"])
    monkeypatch.setitem(sys.modules, "webview", mod)
    res = _JsApi().select_path("file")
    assert res == [r"C:\mods\m.jar"]
    assert win.calls and win.calls[0][0] == (2,)   # webview.OPEN_DIALOG
    assert "file_types" in win.calls[0][1]         # 文件过滤参数已传入


def test_jsapi_cancel_returns_empty(monkeypatch):
    # 用户取消对话框 → create_file_dialog 返回 None → 方法回 [] 而非报错
    mod, win = _fake_webview(None)
    monkeypatch.setitem(sys.modules, "webview", mod)
    assert _JsApi().select_path("folder") == []


def test_jsapi_invalid_kind(monkeypatch):
    # 非法 kind：不弹对话框直接返回空（kind 校验逻辑）
    # 故意不注入 webview：方法应先校验 kind，非法值不得触达 webview import/调用
    assert _JsApi().select_path("bogus") == []
