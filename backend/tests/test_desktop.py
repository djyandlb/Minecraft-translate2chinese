# 任务 O2 测试：/api/key/status 端点（keyring 读取，不返回 key）+ desktop._JsApi 对话框选择逻辑
import sys
import types
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
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
    mod = types.SimpleNamespace(windows=[win], FOLDER_DIALOG=1, OPEN_DIALOG=2, SAVE_DIALOG=3)
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


# —— desktop._JsApi.save_output（桌面版下载：SAVE_DIALOG + 从 OUTPUTS_DIR 读产物写用户位置）——

def _make_outputs(tmp_path, task_id, files: dict[str, bytes]) -> Path:
    """在 tmp_path 下搭 OUTPUTS_DIR，写入给定产物文件（相对路径 → 字节）。"""
    out_dir = tmp_path / "outputs"
    for rel, data in files.items():
        p = out_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return out_dir


def test_jsapi_save_output_modjar(tmp_path, monkeypatch):
    # modjar：OUTPUTS_DIR/<task_id>/<原mod名>-简体中文化.jar → 弹保存框（默认名=该 jar），直接复制到用户选的位置
    task_id = "a1b2c3d4e5f6"
    jar_bytes = b"PK-jar-content"
    outputs = _make_outputs(tmp_path, task_id, {f"{task_id}/mod-简体中文化.jar": jar_bytes})
    monkeypatch.setattr(main, "OUTPUTS_DIR", outputs)
    dest = tmp_path / "saved" / "我的mod.jar"
    mod, win = _fake_webview(str(dest))
    monkeypatch.setitem(sys.modules, "webview", mod)
    res = _JsApi().save_output(task_id)
    assert res == {"ok": True, "path": str(dest)}
    assert win.calls and win.calls[0][0] == (3,)                    # webview.SAVE_DIALOG
    assert win.calls[0][1]["save_filename"] == "mod-简体中文化.jar"  # 默认名 = 产物实际文件名
    assert dest.read_bytes() == jar_bytes                            # 内容已复制到用户位置


def test_jsapi_save_output_modpack(tmp_path, monkeypatch):
    # modpack：资源包+补丁包多文件 → 打包总 zip（与 /api/download 一致），默认名 {task_id}.zip
    task_id = "a1b2c3d4e5f6"
    outputs = _make_outputs(tmp_path, task_id, {
        f"{task_id}/模组汉化资源包.zip": b"zip-a",
        f"{task_id}/汉化补丁包.zip": b"zip-b",
    })
    monkeypatch.setattr(main, "OUTPUTS_DIR", outputs)
    dest = tmp_path / "saved" / "pack.zip"
    mod, win = _fake_webview(str(dest))
    monkeypatch.setitem(sys.modules, "webview", mod)
    res = _JsApi().save_output(task_id)
    assert res["ok"] is True
    assert win.calls[0][1]["save_filename"] == f"{task_id}.zip"
    with zipfile.ZipFile(dest) as zf:
        names = zf.namelist()
    assert "模组汉化资源包.zip" in names and "汉化补丁包.zip" in names


def test_jsapi_save_output_map(tmp_path, monkeypatch):
    # map：OUTPUTS_DIR/<task_id>_<lang>.mcworld → 弹保存框，默认名 = mcworld 文件名
    task_id = "a1b2c3d4e5f6"
    mc_bytes = b"world-data"
    outputs = _make_outputs(tmp_path, task_id, {f"{task_id}_zh_cn.mcworld": mc_bytes})
    monkeypatch.setattr(main, "OUTPUTS_DIR", outputs)
    dest = tmp_path / "saved" / "地图.mcworld"
    mod, win = _fake_webview(str(dest))
    monkeypatch.setitem(sys.modules, "webview", mod)
    res = _JsApi().save_output(task_id)
    assert res == {"ok": True, "path": str(dest)}
    assert win.calls[0][1]["save_filename"] == f"{task_id}_zh_cn.mcworld"
    assert dest.read_bytes() == mc_bytes


def test_jsapi_save_output_cancel(tmp_path, monkeypatch):
    # 用户取消保存框 → create_file_dialog 返回 None → 返回取消，不写文件
    outputs = _make_outputs(tmp_path, "a1b2c3d4e5f6", {"a1b2c3d4e5f6/m.jar": b"x"})
    monkeypatch.setattr(main, "OUTPUTS_DIR", outputs)
    mod, win = _fake_webview(None)
    monkeypatch.setitem(sys.modules, "webview", mod)
    res = _JsApi().save_output("a1b2c3d4e5f6")
    assert res == {"ok": False, "error": "已取消保存"}


def test_jsapi_save_output_invalid_task(monkeypatch):
    # 非法 task_id：不弹对话框直接返回错误（不触达 webview）
    # 故意不注入 webview：校验失败不应触发 webview import/调用
    res = _JsApi().save_output("../etc")
    assert res == {"ok": False, "error": "任务不存在"}


def test_jsapi_save_output_no_output(tmp_path, monkeypatch):
    # 合法 task_id 但产物不存在 → 返回错误，且不得弹保存框（无产物不应打扰用户）
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    monkeypatch.setattr(main, "OUTPUTS_DIR", outputs)
    mod, win = _fake_webview(str(tmp_path / "nope.jar"))
    monkeypatch.setitem(sys.modules, "webview", mod)
    res = _JsApi().save_output("a1b2c3d4e5f6")
    assert res["ok"] is False and "产物" in res["error"]
    assert win.calls == []   # 未调用 create_file_dialog
