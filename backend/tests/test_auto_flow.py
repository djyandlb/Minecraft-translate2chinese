# -*- coding: utf-8 -*-
"""A5 统一全自动翻译流程（auto_flow.py）测试。

验证 run_auto_translation：
  - modpack 语言文件 + 硬编码并入：产物 = 资源包 zip + 汉化 hardcoded jar（javac 真实编译）
  - map 委托 run_map_translation
  - download 端点：产物目录优先打包总 zip，旧单文件兼容
核心铁律：原 jar 只读（硬编码 replace 前 copy2 到 out_dir/hardcoded/<name> 副本再改）。
"""

import asyncio
import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.auto_flow import run_auto_translation
from app.detect import detect_input_type
from app.hardcode import scan_hardcoded_strings
from app.tasks import TaskStore


class _FakeEngine:
    """假翻译引擎：Hello World → 你好世界，Welcome → 欢迎。"""

    def __init__(self):
        pass

    async def translate_batch(self, texts, target_lang):
        return [t.replace("Hello World", "你好世界").replace("Welcome", "欢迎") for t in texts]


def _make_mod_jar(tmp_path, name="mod.jar", lang="en_us"):
    """造含语言文件的 mod jar（无 class）。"""
    jar = tmp_path / name
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("assets/mymod/lang/en_us.json", json.dumps({"key.hello": "Hello World"}))
    return jar


def _make_jar_with_hardcode(tmp_path, name="mod.jar"):
    """javac 编译含硬编码字符串的类打包（无 javac 则 skip）。"""
    if shutil.which("javac") is None:
        pytest.skip("无 javac")
    src = tmp_path / "src"
    src.mkdir()
    (src / "HelloMod.java").write_text(
        'public class HelloMod { public static void main(String[] a) { System.out.println("Hello World"); } }',
        encoding="utf-8")
    cls = tmp_path / "cls"
    cls.mkdir()
    subprocess.run(["javac", "-d", str(cls), str(src / "HelloMod.java")], check=True)
    jar = tmp_path / name
    with zipfile.ZipFile(jar, "w") as zf:
        for f in cls.rglob("*.class"):
            zf.write(f, f.relative_to(cls).as_posix())
    return jar


@pytest.mark.asyncio
async def test_auto_modjar_lang_and_hardcode(tmp_path, monkeypatch):
    """整合包目录：语言文件 mod + 硬编码 mod 一起翻译，产物资源包 + hardcoded jar。"""
    mods = tmp_path / "mods"
    mods.mkdir()
    _make_mod_jar(mods)                                  # 语言文件 mod
    _make_jar_with_hardcode(mods, name="h.jar")          # 硬编码 mod
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: _FakeEngine())
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, None, store, work)
    assert store.load(state.id).status == "done"
    # 产物：资源包 + hardcoded jar
    out_dir = work / "outputs" / state.id
    packs = list(out_dir.glob("*_zh_cn.zip"))
    hards = list((out_dir / "hardcoded").glob("*.jar"))
    assert packs and hards, f"产物缺失 packs={packs} hards={hards}"
    # 汉化 jar 内字符串已被替换（副本被改，原 jar 只读）
    assert "你好世界" in " ".join(scan_hardcoded_strings(hards[0]))
    # 资源包内含汉化词条
    with zipfile.ZipFile(packs[0]) as zf:
        data = json.loads(zf.read("assets/mymod/lang/zh_cn.json").decode("utf-8"))
    assert data["key.hello"] == "你好世界"


@pytest.mark.asyncio
async def test_auto_same_script_zh_cn_to_zh_tw(tmp_path, monkeypatch):
    """简繁互转场景：源中文不能被 needs_translation 误判「已汉化」跳过，必须转繁体（回归 bug 修复）。"""
    mods = tmp_path / "mods"
    mods.mkdir()
    with zipfile.ZipFile(mods / "m.jar", "w") as zf:
        zf.writestr("assets/mymod/lang/zh_cn.json", json.dumps({"k": "机器翻译"}))
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: _FakeEngine())
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_tw", source_lang="zh_cn")
    await run_auto_translation(state.id, req, None, store, work)
    assert store.load(state.id).status == "done"
    packs = list((work / "outputs" / state.id).glob("*_zh_tw.zip"))
    assert packs
    with zipfile.ZipFile(packs[0]) as zf:
        data = json.loads(zf.read("assets/mymod/lang/zh_tw.json").decode("utf-8"))
    assert data["k"] == "機器翻譯"


@pytest.mark.asyncio
async def test_auto_map_delegates(tmp_path, monkeypatch):
    """地图输入 → 委托 run_map_translation（含 MapTranslateRequest 构造）。"""
    from nbtlib import File, Compound, String
    w = tmp_path / "world"
    w.mkdir()
    File({"Data": Compound({"Command": String("say Hello")})}).save(w / "level.dat", gzipped=True)
    assert detect_input_type(w) == "map"
    called = {}

    async def fake_map(task_id, req, cfg, store, work_dir):
        called["req"] = req

    monkeypatch.setattr("app.auto_flow.run_map_translation", fake_map)
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    req = SimpleNamespace(path=str(w), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, None, store, work)
    assert called and called["req"].path == str(w)


def test_download_packs_output_dir(tmp_path, monkeypatch):
    """download：产物目录存在时打包总 zip（含 hardcoded jar），旧单文件兼容兜底。"""
    import app.main as main
    from fastapi.testclient import TestClient

    out_dir = tmp_path / "work" / "outputs" / "abc123def456"
    out_dir.mkdir(parents=True)
    (out_dir / "abc123def456_zh_cn.zip").write_bytes(b"packdata")
    hard = out_dir / "hardcoded"
    hard.mkdir()
    (hard / "abc123def456_h.jar").write_bytes(b"jardata")
    monkeypatch.setattr(main, "WORK_DIR", tmp_path / "work")
    client = TestClient(main.app)
    r = client.get("/api/task/abc123def456/download")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = zf.namelist()
    assert "abc123def456_zh_cn.zip" in names
    assert "hardcoded/abc123def456_h.jar" in names


def test_download_fallback_single_file(tmp_path, monkeypatch):
    """download：无产物目录时回退旧单文件匹配（地图等产物平铺 outputs/）。"""
    import app.main as main
    from fastapi.testclient import TestClient

    (tmp_path / "work" / "outputs").mkdir(parents=True)
    (tmp_path / "work" / "outputs" / "fedcba987654_zh_cn.mcworld").write_bytes(b"world")
    monkeypatch.setattr(main, "WORK_DIR", tmp_path / "work")
    client = TestClient(main.app)
    r = client.get("/api/task/fedcba987654/download")
    assert r.status_code == 200
    assert "fedcba987654_zh_cn.mcworld" in r.headers.get("content-disposition", "")
