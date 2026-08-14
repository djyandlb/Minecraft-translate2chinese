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
from app.cleanup import cleanup_task_work
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


def _pack_zip(out_dir) -> dict[str, bytes]:
    """读产物区成品「整合包汉化.zip」，返回 {相对路径: bytes}。

    产物文件夹重构（用户诉求）：整合包散装（resourcepacks/mods/补丁/使用说明）只在组装区
    组织、只进成品 zip；产物区 outputs/<task_id>/ 只留 zip + report.json，不再一地散装。
    因此断言散装内容一律从 zip 里读。
    """
    z = out_dir / "整合包汉化.zip"
    assert z.exists(), f"产物区应只留成品 zip（+ report.json），实际: {sorted(p.name for p in out_dir.iterdir())}"
    with zipfile.ZipFile(z) as zf:
        return {n: zf.read(n) for n in zf.namelist()}


def _vp_pairs(out_dir):
    """读整合包 VP 硬编码映射 pairs（{key: value}）——整合包硬编码走 VP 补丁形式，
    映射模块在成品 zip 内（vaultpatcher/modules/）。"""
    vp_map = json.loads(_pack_zip(out_dir)["vaultpatcher/modules/mc-auto-translator.json"])
    return {p["key"]: p["value"] for p in vp_map[1]["pairs"]}


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
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    assert store.load(state.id).status == "done"
    # 产物：成品 zip 内含资源包 + VP 硬编码映射（整合包硬编码走 VP 补丁形式，不产修改版 jar）
    out_dir = outputs / state.id
    pk = _pack_zip(out_dir)
    # 产物区只留成品 zip（+ report.json），不再一地散装（用户诉求）
    loose = {p.name for p in out_dir.iterdir() if p.name not in ("整合包汉化.zip", "report.json")}
    assert not loose, f"产物文件夹应只剩成品 zip，实际散装: {loose}"
    # 硬编码翻译进 VP 映射（vaultpatcher/modules/），运行时注入不碰 mod jar
    pairs = _vp_pairs(out_dir)
    assert pairs.get("Hello World") == "你好世界", "硬编码翻译应进 VP 映射"
    # 整合包不产修改版 jar（硬编码走 VP 补丁形式，无二次分发纠纷）
    assert not (out_dir / "hardcoded").exists()
    # 资源包内含汉化词条
    data = json.loads(pk["resourcepacks/模组汉化资源包/assets/mymod/lang/zh_cn.json"])
    assert data["key.hello"] == "你好世界"


class _SnakeEngine:
    """假引擎：snake_case 语言文件值 Requires_Armor → 需要盔甲。"""

    async def translate_batch(self, texts, target_lang):
        return [t.replace("Requires_Armor", "需要盔甲") for t in texts]


@pytest.mark.asyncio
async def test_auto_lang_snake_case_value_translated(tmp_path, monkeypatch):
    """语言文件 snake_case 值（Requires_Armor）真正被翻译：
    阶段 1 关闭技术串过滤 + 只判已汉化，值进入引擎并写回产物。"""
    mods = tmp_path / "mods"
    mods.mkdir()
    with zipfile.ZipFile(mods / "m.jar", "w") as zf:
        zf.writestr("assets/mymod/lang/en_us.json", json.dumps({"item.armor": "Requires_Armor"}))
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: _SnakeEngine())
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    assert store.load(state.id).status == "done"
    data = json.loads(_pack_zip(outputs / state.id)["resourcepacks/模组汉化资源包/assets/mymod/lang/zh_cn.json"])
    assert data["item.armor"] == "需要盔甲"


@pytest.mark.asyncio
async def test_auto_effect_ai_judged_not_forced_english(tmp_path, monkeypatch):
    """effect.* 不再一刀切英文（Xaero 审查修复）：交 AI 翻译并自主判断。
    - AI 能翻译的效果名（Hello World → 你好世界）写回中文（此前强制英文）；
    - AI 保留原文的效果名（No Minimap 假引擎不识别）不记 failed（keep_original_ok），
      写回原文覆盖旧中文（防 Identifier 崩的兜底）。
    """
    mods = tmp_path / "mods"
    mods.mkdir()
    with zipfile.ZipFile(mods / "m.jar", "w") as zf:
        zf.writestr("assets/mymod/lang/en_us.json", json.dumps({
            "effect.mymod.status": "Hello World",
            "effect.mymod.no_minimap": "No Minimap",
            "gui.title": "Welcome",
        }))
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: _FakeEngine())
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    st = store.load(state.id)
    assert st.status == "done"
    assert st.failed == 0            # AI 保留原文不算失败（keep_original_ok）
    data = json.loads(_pack_zip(outputs / state.id)["resourcepacks/模组汉化资源包/assets/mymod/lang/zh_cn.json"])
    assert data.get("effect.mymod.status") == "你好世界"      # AI 判断可翻译 → 中文
    assert data.get("effect.mymod.no_minimap") == "No Minimap"  # AI 保留原文 → 英文
    assert data.get("gui.title") == "欢迎"


@pytest.mark.asyncio
async def test_auto_keep_original_not_failed(tmp_path, monkeypatch):
    """AI 保留原文（专有名词 Minecraft）不记 failed——「译文存在即成功」。
    修复：32 条未翻译里 Minecraft 等 AI 故意保留的专有名词被误判「LLM 未返回文本」。"""
    mods = tmp_path / "mods"
    mods.mkdir()
    with zipfile.ZipFile(mods / "m.jar", "w") as zf:
        zf.writestr("assets/mymod/lang/en_us.json", json.dumps({
            "title.minecraft": "Minecraft",          # 专有名词：AI 保留原文
            "gui.hello": "Hello World",              # 正常翻译
        }))

    class _KeepEngine:
        async def translate_batch(self, texts, target_lang):
            return [t.replace("Hello World", "你好世界") for t in texts]

    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: _KeepEngine())
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    st = store.load(state.id)
    assert st.status == "done"
    assert st.failed == 0            # Minecraft 保留原文不误报 failed
    data = json.loads(_pack_zip(outputs / state.id)["resourcepacks/模组汉化资源包/assets/mymod/lang/zh_cn.json"])
    assert data["title.minecraft"] == "Minecraft"    # 保留原文写入产物
    assert data["gui.hello"] == "你好世界"


@pytest.mark.asyncio
async def test_auto_ai_review_retry_loop(tmp_path, monkeypatch):
    """AI 质量审查（裁判核心）：不合格条目 force_engine 重翻 → 重翻后终审合格不记 failed。
    验证合并后的统一审查闭环：AI 审查抓出劣质译文 → 强制重翻覆盖 → 终审通过。"""
    from app.translate.llm import LLMClient

    mods = tmp_path / "mods"
    mods.mkdir()
    with zipfile.ZipFile(mods / "m.jar", "w") as zf:
        zf.writestr("assets/mymod/lang/en_us.json", json.dumps({
            "gui.a": "Hello World",
            "gui.b": "Welcome Home",
        }))
    engine = LLMClient("https://x", "k", "m")
    calls = {"n": 0}
    async def fake_translate(texts, target, forced=False, feedback=None, meta=None):
        calls["n"] += 1
        if meta is not None:
            # 适配：LLM 引擎 translate_batch 现在带 meta 收 per-call 失败状态
            meta.update({"failed": set(), "kind": "other", "fatal": None})
        if calls["n"] == 1:
            return ["你好世界", "欢迎回家"]      # 首轮译完（gui.a 会被审不合格）
        return ["你好世界！", "欢迎回家"]        # 重翻改进（更完整）
    engine.translate_batch = fake_translate
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: engine)
    # 审查：第 1 次报 gui.a 译文不完整；重翻后再审全部合格
    review_calls = {"n": 0}
    async def fake_review(eng, pairs, target_lang, **kw):
        review_calls["n"] += 1
        if review_calls["n"] == 1:
            return [{"key": p["key"], "source": p["source"],
                     "translated": p["translated"], "reason": "译文不完整"}
                    for p in pairs if p["key"] == "gui.a"]
        return []
    monkeypatch.setattr("app.auto_flow.review_translations", fake_review)
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    st = store.load(state.id)
    assert st.status == "done"
    assert st.failed == 0                       # 重翻后终审合格，不误记 failed
    assert review_calls["n"] >= 1               # v1.2.7 轻量化：只初审一次，重翻后不再 AI 再审
    data = json.loads(_pack_zip(outputs / state.id)["resourcepacks/模组汉化资源包/assets/mymod/lang/zh_cn.json"])
    assert data["gui.a"] == "你好世界！"          # 劣质译文被重翻覆盖
    assert data["gui.b"] == "欢迎回家"


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
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_tw", source_lang="zh_cn")
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    assert store.load(state.id).status == "done"
    data = json.loads(_pack_zip(outputs / state.id)["resourcepacks/模组汉化资源包/assets/mymod/lang/zh_tw.json"])
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

    async def fake_map(task_id, req, cfg, store, work_dir, outputs_dir):
        called["req"] = req
        called["outputs_dir"] = outputs_dir

    monkeypatch.setattr("app.auto_flow.run_map_translation", fake_map)
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(w), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    assert called and called["req"].path == str(w)
    assert called["outputs_dir"] == outputs   # map 委托也透传产物目录


def test_download_packs_output_dir(tmp_path, monkeypatch):
    """download：产物区顶层成品 zip（整合包汉化.zip）→ 直接返回，不再 rglob 重打包。"""
    import app.main as main
    from fastapi.testclient import TestClient

    out_dir = tmp_path / "outputs" / "abc123def456"
    out_dir.mkdir(parents=True)
    (out_dir / "整合包汉化.zip").write_bytes(b"zipdata")
    (out_dir / "report.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(main, "OUTPUTS_DIR", tmp_path / "outputs")
    client = TestClient(main.app)
    r = client.get("/api/task/abc123def456/download")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/zip")
    assert r.content == b"zipdata"   # 成品 zip 原样返回（不重新打包）


@pytest.mark.asyncio
async def test_auto_same_stem_jars_dedup(tmp_path, monkeypatch):
    """不同子目录同名 jar（stem 相同）→ 两个汉化 jar 都产出（文件名去重不覆盖）。"""
    mods = tmp_path / "mods"
    (mods / "a").mkdir(parents=True)
    (mods / "b").mkdir(parents=True)
    _make_jar_with_hardcode(mods / "a", name="mod.jar")
    _make_jar_with_hardcode(mods / "b", name="mod.jar")
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: _FakeEngine())
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    assert store.load(state.id).status == "done"
    # 整合包硬编码走 VP 映射（多个 jar 的硬编码合并进同一映射，不产修改版 jar）
    assert not list((outputs / state.id / "hardcoded").glob("*.jar"))
    pairs = _vp_pairs(outputs / state.id)
    assert pairs, "VP 映射应有硬编码翻译（多个 jar 合并）"


@pytest.mark.asyncio
async def test_auto_all_hanzified_no_pack(tmp_path, monkeypatch):
    """全部已汉化（源语言检测为 None → 无空缺）→ done + warn，不导出空产物。"""
    mods = tmp_path / "mods"
    mods.mkdir()
    with zipfile.ZipFile(mods / "m.jar", "w") as zf:
        zf.writestr("assets/mymod/lang/zh_cn.json", json.dumps({"k": "已汉化"}))
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: _FakeEngine())
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    assert store.load(state.id).status == "done"
    out_dir = outputs / state.id
    # 无可导出产物：无资源包 zip，hardcoded 目录不存在或为空
    assert not list(out_dir.glob("*.zip"))
    hard_dir = out_dir / "hardcoded"
    assert not hard_dir.exists() or not list(hard_dir.glob("*.jar"))


@pytest.mark.asyncio
async def test_auto_engine_exception_does_not_kill_flow(tmp_path, monkeypatch):
    """引擎异常（网络/API 失败）→ 等待网络恢复重试，恢复后成功不记 failed（用户铁律：不跳过）。"""
    calls = [0]
    class _FlakyEngine:
        _fatal_error = None
        _batch_failed_texts = set()
        _last_error_kind = "network"
        async def translate_batch(self, texts, target_lang):
            calls[0] += 1
            if calls[0] <= 2:   # 前两次抛异常（名字翻译 + 语言文件主请求）模拟网络中断
                raise RuntimeError("网络中断")
            return [t.replace("Hello", "你好") for t in texts]
    mods = tmp_path / "mods"
    mods.mkdir()
    _make_mod_jar(mods)  # 语言文件 mod（含 "Hello World"）
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: _FlakyEngine())
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    st = store.load(state.id)
    assert st.status == "done"   # 网络恢复后正常完成
    assert st.failed == 0        # 网络恢复后不记 failed（不跳过）


def test_download_fallback_single_file(tmp_path, monkeypatch):
    """download：无产物目录时回退旧单文件匹配（地图等产物平铺 outputs/）。"""
    import app.main as main
    from fastapi.testclient import TestClient

    (tmp_path / "outputs").mkdir(parents=True)
    (tmp_path / "outputs" / "fedcba987654_zh_cn.mcworld").write_bytes(b"world")
    monkeypatch.setattr(main, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(main, "OUTPUTS_DIR", tmp_path / "outputs")
    client = TestClient(main.app)
    r = client.get("/api/task/fedcba987654/download")
    assert r.status_code == 200
    assert "fedcba987654_zh_cn.mcworld" in r.headers.get("content-disposition", "")


def _make_jar_with_two_hardcode(tmp_path, name="two.jar"):
    """javac 编译含两个硬编码字符串（"Hello World" 与 "Welcome"）的类打包（无 javac 则 skip）。"""
    if shutil.which("javac") is None:
        pytest.skip("无 javac")
    src = tmp_path / "src2"
    src.mkdir()
    (src / "TwoMod.java").write_text(
        'public class TwoMod { public static void main(String[] a) { '
        'System.out.println("Hello World"); System.out.println("Welcome"); } }',
        encoding="utf-8")
    cls = tmp_path / "cls2"
    cls.mkdir()
    subprocess.run(["javac", "-d", str(cls), str(src / "TwoMod.java")], check=True)
    jar = tmp_path / name
    with zipfile.ZipFile(jar, "w") as zf:
        for f in cls.rglob("*.class"):
            zf.write(f, f.relative_to(cls).as_posix())
    return jar


@pytest.mark.asyncio
async def test_auto_selected_hardcoded_ignored(tmp_path, monkeypatch):
    """取消 selected_hardcoded 生效：即使只传 "Hello World"，硬编码仍全翻（不再手动勾选）。"""
    mods = tmp_path / "mods"
    mods.mkdir()
    _make_jar_with_two_hardcode(mods, name="two.jar")
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: _FakeEngine())
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None,
                          selected_hardcoded=["Hello World"])
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    assert store.load(state.id).status == "done"
    pairs = _vp_pairs(outputs / state.id)
    assert pairs, "VP 映射应有硬编码翻译"
    assert pairs.get("Hello World") == "你好世界"   # Hello World 已翻译
    assert pairs.get("Welcome") == "欢迎"          # Welcome 也被翻译（selected_hardcoded 不再生效）


@pytest.mark.asyncio
async def test_auto_machine_skips_hardcode(tmp_path, monkeypatch):
    """machine 引擎：硬编码跳过 + progress warn，但语言文件仍正常翻译成资源包。"""
    from app.translate.machine import MachineClient

    mods = tmp_path / "mods"
    mods.mkdir()
    _make_mod_jar(mods)                                    # 语言文件 mod
    _make_jar_with_hardcode(mods, name="h.jar")            # 硬编码 mod（应被跳过）

    eng = MachineClient(provider="google")
    async def fake_batch(texts, target_lang):
        return [t.replace("Hello World", "你好世界") for t in texts]
    eng.translate_batch = fake_batch
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: eng)

    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    st = store.load(state.id)
    assert st.status == "done"
    # machine 引擎 total 不含硬编码候选（只含语言文件 1 词条 + build 1 单位）
    assert st.total == 3   # lang 1 + build 2（资源包目录 + 整合包汉化.zip）
    assert st.done == st.total                     # build 阶段推进到 100%
    # 阶段结构：lang 1 + build 1，无 hardcode 阶段（machine 不扫硬编码）
    stages = {s["name"]: s for s in st.stages}
    assert stages["lang"]["total"] == 1 and stages["lang"]["done"] == 1
    assert "hardcode" not in stages
    assert stages["build"]["done"] == stages["build"]["total"]
    # 语言文件正常翻 → 资源包产出（在成品 zip 内，散装只进 zip）
    data = json.loads(_pack_zip(outputs / state.id)["resourcepacks/模组汉化资源包/assets/mymod/lang/zh_cn.json"])
    assert data["key.hello"] == "你好世界"
    # 硬编码跳过：产物区无散装 hardcoded 目录（组装区产物只进 zip）+ warn 明确提示
    assert not (outputs / state.id / "hardcoded").exists()
    warns = [p for p in st.progress if p.get("status") == "warn"]
    assert any("硬编码" in str(p.get("error", "")) for p in warns), "machine 跳过硬编码应有 warn 提示"


class _JsonLinesEngine:
    """假引擎：结构化 json / md 行文本专用翻译。"""

    async def translate_batch(self, texts, target_lang):
        m = {"Welcome": "欢迎", "Welcome to the mod": "欢迎来到本模组"}
        return [m.get(t, t) for t in texts]


@pytest.mark.asyncio
async def test_auto_json_lines_written_back(tmp_path, monkeypatch):
    """json/lines 全文本覆盖：mod jar 含结构化 json + en_us md → 汉化 jar 内写回译文。"""
    mods = tmp_path / "mods"
    mods.mkdir()
    jar = mods / "j.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("data/mymod/advancements/title.json",
                    json.dumps({"display": {"title": {"text": "Welcome"}}}))
        zf.writestr("assets/mymod/patchouli_books/guide/en_us/entries/intro.md",
                    "# Intro\r\nWelcome to the mod\r\n")
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: _JsonLinesEngine())
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    assert store.load(state.id).status == "done"
    # 整合包 jar 内 json/lines **不产 hardcoded 修改版 jar**（用户刚需：全走资源包/补丁形式）——
    # 分流：data/ 进补丁包、assets/ 进资源包（均在成品 zip 内）
    pk = _pack_zip(outputs / state.id)
    assert not any(n.startswith("hardcoded/") for n in pk), "整合包不应产 hardcoded 修改版 jar"
    data = json.loads(pk["data/mymod/advancements/title.json"])      # data/ → 补丁包
    assert data["display"]["title"]["text"] == "欢迎"                 # 结构化 json（advancements）译文
    md = pk["resourcepacks/模组汉化资源包/assets/mymod/patchouli_books/guide/zh_cn/entries/intro.md"].decode("utf-8")  # assets/ → 资源包
    assert "欢迎来到本模组" in md                                     # lines 译文写回 zh_cn 路径
    assert "# Intro" in md                                            # 未翻译行保留


@pytest.mark.asyncio
async def test_auto_llm_ai_judge_only_visible(tmp_path, monkeypatch):
    """LLM 引擎：ai_judge_translate 只替换 AI 判定可见的硬编码（mock ai_judge）。"""
    from app.translate.llm import LLMClient

    mods = tmp_path / "mods"
    mods.mkdir()
    _make_jar_with_two_hardcode(mods, name="two.jar")   # "Hello World" + "Welcome"
    engine = LLMClient("https://x", "k", "m")
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: engine)

    async def fake_judge(engine, candidates, target, known_translations=None,
                         on_batch_done=None, on_batch_start=None,
                         silly_mode=False):
        if on_batch_start:
            on_batch_start(len(candidates))
        if on_batch_done:
            on_batch_done(len(candidates))
        return {"Hello World": "你好世界"}   # AI 只判定 Hello World 可见

    monkeypatch.setattr("app.auto_flow.ai_judge_translate", fake_judge)
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    st = store.load(state.id)
    assert st.status == "done"
    pairs = _vp_pairs(outputs / state.id)
    assert pairs, "LLM AI 判断应产出 VP 映射"
    assert pairs.get("Hello World") == "你好世界"   # AI 判定可见 → 翻译进 VP 映射
    assert pairs.get("Welcome") != "欢迎"          # AI 判定不可见 → 不翻译
    # AI 判定不可翻译数量有 warn 提示
    warns = [p for p in st.progress if p.get("status") == "warn"]
    assert any("非用户可见" in str(p.get("error", "")) for p in warns)
    # LLM 分支补汇总 progress（🔵6）：judged 数 = 候选数，visible 数 = 映射数
    done_items = [p for p in st.progress if p.get("status") == "done" and "judged" in p]
    assert done_items, "LLM 硬编码批处理应有汇总 progress 记录"
    assert done_items[0]["judged"] == 2 and done_items[0]["visible"] == 1


@pytest.mark.asyncio
async def test_auto_llm_ai_judge_failure_no_double_count(tmp_path, monkeypatch):
    """LLM 引擎：ai_judge 整批抛异常 → 仅计 failed 不重复计 done（done+failed 不超 total）（B 审查 🟡4）。"""
    from app.translate.llm import LLMClient

    mods = tmp_path / "mods"
    mods.mkdir()
    _make_jar_with_two_hardcode(mods, name="two.jar")   # "Hello World" + "Welcome" 两条候选
    engine = LLMClient("https://x", "k", "m")
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: engine)

    async def boom_judge(engine, candidates, target, **kwargs):
        raise RuntimeError("LLM 服务不可用")

    monkeypatch.setattr("app.auto_flow.ai_judge_translate", boom_judge)
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    st = store.load(state.id)
    assert st.status == "done"
    assert st.failed >= 1                       # 异常整批计入 failed
    assert st.done + st.failed <= st.total      # 不双计：done+failed 不超 total
    hard_dir = outputs / state.id / "hardcoded"
    assert not hard_dir.exists() or not list(hard_dir.glob("*.jar"))


@pytest.mark.asyncio
async def test_auto_llm_ai_judge_cancel_between_jars(tmp_path, monkeypatch):
    """LLM 引擎：多 jar 硬编码批处理中取消 → 下一个 jar 前停下，状态 cancelled（B 审查 🟡3）。"""
    from app.translate.llm import LLMClient

    mods = tmp_path / "mods"
    (mods / "a").mkdir(parents=True)
    (mods / "b").mkdir(parents=True)
    _make_jar_with_hardcode(mods / "a", name="a.jar")
    _make_jar_with_hardcode(mods / "b", name="b.jar")
    engine = LLMClient("https://x", "k", "m")
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: engine)

    calls = {"n": 0}

    async def judge_then_cancel(engine, candidates, target, known_translations=None,
                                on_batch_done=None, on_batch_start=None,
                                silly_mode=False):
        calls["n"] += 1
        # 处理完第一个 jar 后立即置取消（TaskStore 缓存使 auto_flow 与本测试共享同一 state 对象）
        if calls["n"] == 1:
            state.cancelled = True
            store.save(state)
        if on_batch_done:
            on_batch_done(len(candidates))   # 逐批进度回调（进度条实时推进）
        return {"Hello World": "你好世界"}

    monkeypatch.setattr("app.auto_flow.ai_judge_translate", judge_then_cancel)
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    st = store.load(state.id)
    assert st.status == "cancelled"
    assert calls["n"] == 1        # 第二个 jar 前被取消拦截，不再调用 ai_judge


def test_cleanup_task_work_keeps_global(tmp_path):
    """cleanup_task_work：删任务级临时子目录（jars/extracted/maps/uploads 的 <task_id>），
    保留全局 memory.json/glossary.json/tasks 与产物 OUTPUTS_DIR。"""
    work = tmp_path / "work"
    tid = "abc123def456"
    for sub in ("jars", "extracted", "maps", "uploads"):
        (work / sub / tid).mkdir(parents=True)
    # 全局文件/目录（任务状态轮询依赖）必须保留
    (work / "memory.json").write_text("{}", encoding="utf-8")
    (work / "glossary.json").write_text("[]", encoding="utf-8")
    (work / "tasks").mkdir(parents=True)
    cleanup_task_work(work, tid)
    for sub in ("jars", "extracted", "maps", "uploads"):
        assert not (work / sub / tid).exists(), f"{sub} 任务级目录未清理"
    assert (work / "memory.json").exists()
    assert (work / "glossary.json").exists()
    assert (work / "tasks").exists()


@pytest.mark.asyncio
async def test_auto_cleans_task_work_keeps_outputs(tmp_path, monkeypatch):
    """任务 done 后：WORK_DIR 任务级中间目录被清理，OUTPUTS_DIR 产物保留（download 依赖）。"""
    mods = tmp_path / "mods"
    mods.mkdir()
    _make_mod_jar(mods)                                  # 语言文件 mod
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: _FakeEngine())
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    # 预置任务级临时目录，验证 flow 收尾会清掉
    for sub in ("jars", "extracted", "maps", "uploads"):
        (work / sub / state.id).mkdir(parents=True)
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, None, store, work, outputs)
    assert store.load(state.id).status == "done"
    # 中间产物已清理（含 build 组装区——散装打完 zip 后整体清理）
    for sub in ("jars", "extracted", "maps", "uploads", "build"):
        assert not (work / sub / state.id).exists(), f"{sub} 任务后未清理"
    # 产物保留在 OUTPUTS_DIR（只剩成品 zip + report.json，散装只进 zip 已清）
    assert (outputs / state.id / "整合包汉化.zip").exists(), "产物 zip 应保留"


# ---------- 产物形态改造：mod→汉化jar / 整合包→资源包 + 汉化命名 ----------

def _add_hardcode_to_jar(jar: Path):
    """给已有 jar 追加硬编码 class（javac 编译），无 javac 则 skip。"""
    if shutil.which("javac") is None:
        pytest.skip("无 javac")
    src = jar.parent / "src_h"
    src.mkdir()
    (src / "HelloMod.java").write_text(
        'public class HelloMod { public static void main(String[] a) { System.out.println("Hello World"); } }',
        encoding="utf-8")
    cls = jar.parent / "cls_h"
    cls.mkdir()
    subprocess.run(["javac", "-d", str(cls), str(src / "HelloMod.java")], check=True)
    with zipfile.ZipFile(jar, "a") as zf:
        for f in cls.rglob("*.class"):
            zf.write(f, f.relative_to(cls).as_posix())


@pytest.mark.asyncio
async def test_auto_modjar_outputs_single_jar(tmp_path, monkeypatch):
    """modjar 输入 → 产物为单个汉化 jar（语言文件+硬编码全写回），
    命名 {原jar stem}-简体中文化.jar，无资源包 zip / hardcoded 子目录。"""
    jar = _make_mod_jar(tmp_path, name="mod.jar")      # 语言文件 mod
    _add_hardcode_to_jar(jar)                           # 注入硬编码 class
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: _FakeEngine())
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(jar), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, None, store, work, outputs)
    assert store.load(state.id).status == "done"
    out_dir = outputs / state.id
    # 产物：单个顶层汉化 jar，命名 xxx-简体中文化.jar
    jars = list(out_dir.glob("*.jar"))
    assert len(jars) == 1, f"期望单个汉化 jar，实际 {jars}"
    assert jars[0].name == "mod-简体中文化.jar"
    # 无资源包 zip、无 hardcoded 子目录
    assert not list(out_dir.glob("*.zip"))
    assert not (out_dir / "hardcoded").exists()
    # jar 内含 zh_cn 语言文件（译文写回）+ 硬编码已替换（副本被改，原 jar 只读）
    with zipfile.ZipFile(jars[0]) as zf:
        data = json.loads(zf.read("assets/mymod/lang/zh_cn.json").decode("utf-8"))
    assert data["key.hello"] == "你好世界"
    assert "你好世界" in " ".join(scan_hardcoded_strings(jars[0]))
    # 原 jar 只读：语言文件未被写回，硬编码未替换
    with zipfile.ZipFile(jar) as zf:
        assert "assets/mymod/lang/zh_cn.json" not in zf.namelist()
    assert "你好世界" not in " ".join(scan_hardcoded_strings(jar))


@pytest.mark.asyncio
async def test_auto_modpack_outputs_resource_pack(tmp_path, monkeypatch):
    """modpack 输入 → 资源包 zip + hardcoded jar（现状保留），顶层不落单 jar。"""
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
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    assert store.load(state.id).status == "done"
    out_dir = outputs / state.id
    pk = _pack_zip(out_dir)                    # 散装只进成品 zip
    pairs = _vp_pairs(out_dir)
    assert pairs, "modpack 应产出 VP 映射"
    # 整合包不产修改版 jar（硬编码走 VP 补丁形式，无二次分发纠纷）；产物区只留成品 zip
    assert not list(out_dir.glob("*.jar"))
    assert not (out_dir / "hardcoded").exists()
    data = json.loads(pk["resourcepacks/模组汉化资源包/assets/mymod/lang/zh_cn.json"])
    assert data["key.hello"] == "你好世界"


def test_lang_display_name_mapping():
    """汉化命名映射：zh_cn→简体中文、zh_tw→繁体中文、ja_jp→日文、fr_fr→法文。"""
    from app.auto_flow import lang_display_name
    assert lang_display_name("zh_cn") == "简体中文"
    assert lang_display_name("zh_tw") == "繁体中文"
    assert lang_display_name("ja_jp") == "日文"
    assert lang_display_name("fr_fr") == "法文"


def test_download_modjar_single_jar(tmp_path, monkeypatch):
    """download：modjar 单 jar 产物（顶层一个 jar、无资源包 zip）→ 直接返回该 jar（汉化文件名）。"""
    import urllib.parse
    import app.main as main
    from fastapi.testclient import TestClient

    out_dir = tmp_path / "outputs" / "abc123def456"
    out_dir.mkdir(parents=True)
    (out_dir / "mod-简体中文化.jar").write_bytes(b"jardata")
    monkeypatch.setattr(main, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(main, "OUTPUTS_DIR", tmp_path / "outputs")
    client = TestClient(main.app)
    r = client.get("/api/task/abc123def456/download")
    assert r.status_code == 200
    # Starlette 对非 ASCII 文件名走 RFC 5987 filename*=utf-8''... 编码，解码后应含汉化文件名
    cd = r.headers.get("content-disposition", "")
    assert "mod-简体中文化.jar" in urllib.parse.unquote(cd)
    assert r.content == b"jardata"


# ---------- 批量并发翻译（任务：一次 translate_batch 传多条，不再逐条） ----------

@pytest.mark.asyncio
async def test_auto_translation_batch_call_count(tmp_path, monkeypatch):
    """批量翻译：mock 引擎断言 translate_batch 一次传入多条而非每次 1 条。

    batch_size=2、5 条需翻译词条 → ceil(5/2)=3 次调用；至少一次调用传入 >1 条。
    """
    mods = tmp_path / "mods"
    mods.mkdir()
    with zipfile.ZipFile(mods / "m.jar", "w") as zf:
        zf.writestr("assets/mymod/lang/en_us.json", json.dumps({
            "k0": "Hello Zero", "k1": "Hello One", "k2": "Hello Two",
            "k3": "Hello Three", "k4": "Hello Four",
        }))

    class _BatchSpyEngine:
        batch_size = 2   # 引擎声明的批量上限

        def __init__(self):
            self.calls = []   # 每次 translate_batch 收到的文本列表

        async def translate_batch(self, texts, target_lang):
            self.calls.append(list(texts))
            return [t.replace("Hello", "你好") for t in texts]

    spy = _BatchSpyEngine()
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: spy)
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, None, store, work, outputs)
    assert store.load(state.id).status == "done"
    # 名字翻译（_translate_input_name 翻译目录名）单独 1 次单条小调用，其余为词条批量——
    # 词条批用含 "Hello" 的调用过滤（名字翻译的文本是不含 Hello 的目录名）
    batch_calls = [c for c in spy.calls if any("Hello" in t for t in c)]
    # 批量断言：ceil(5/2)=3 次调用；每次 1-2 条；至少一次 >1 条；总量 = 5
    assert len(batch_calls) == 3, f"期望 3 次批量调用，实际 {len(batch_calls)} 次: {spy.calls}"
    assert all(0 < len(c) <= 2 for c in batch_calls)
    assert any(len(c) > 1 for c in batch_calls), "应至少有一次调用传入多条文本"
    assert sum(len(c) for c in batch_calls) == 5
    # 译文逐条写回产物（批量后产物/进度仍正确）
    data = json.loads(_pack_zip(outputs / state.id)["resourcepacks/模组汉化资源包/assets/mymod/lang/zh_cn.json"])
    assert data["k0"] == "你好 Zero"
    assert data["k4"] == "你好 Four"


# ---------- 整合包全包覆盖：目录文本源（任务线/config/data/kubejs）→ 汉化补丁包 ----------

@pytest.mark.asyncio
async def test_auto_modpack_pack_sources_patch_pack(tmp_path, monkeypatch):
    """整合包目录文本源（config/ftbquests + data + kubejs）→ 汉化补丁包.zip（相对路径 + 使用说明）；
    语言文件 → 模组汉化资源包.zip。"""
    mods = tmp_path / "mods"
    mods.mkdir()
    _make_mod_jar(mods)                                  # 语言文件 mod → 模组汉化资源包
    (tmp_path / "config/ftbquests/quests").mkdir(parents=True)
    (tmp_path / "config/ftbquests/quests/1.json").write_text(
        json.dumps({"title": "Welcome", "item": "minecraft:stone"}), encoding="utf-8")
    (tmp_path / "data/demo/advancements").mkdir(parents=True)
    (tmp_path / "data/demo/advancements/t.json").write_text(
        json.dumps({"display": {"title": {"text": "New World"}}}), encoding="utf-8")
    (tmp_path / "kubejs/server_scripts").mkdir(parents=True)
    (tmp_path / "kubejs/server_scripts/main.js").write_text(
        'console.log("Hello World")\n', encoding="utf-8")
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: _FakeEngine())
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, None, store, work, outputs)
    assert store.load(state.id).status == "done"
    out_dir = outputs / state.id
    pk = _pack_zip(out_dir)                       # 散装（资源包/补丁/mods/使用说明）只进成品 zip
    # 语言文件译文在 zip 内
    data = json.loads(pk["resourcepacks/模组汉化资源包/assets/mymod/lang/zh_cn.json"])
    assert data["key.hello"] == "你好世界"
    # 补丁按整合包相对路径组织在 zip 内（解压即用，无需手动移动）
    quest = json.loads(pk["config/ftbquests/quests/1.json"])
    assert quest["title"] == "欢迎"               # 任务线译文
    assert quest["item"] == "minecraft:stone"      # 技术串原样保留
    assert "data/demo/advancements/t.json" in pk
    assert not any(n.startswith("kubejs/") for n in pk), "回归标准：js 代码不翻不收录"
    # i18n 汉化 mod 内置进 zip 的 mods/（用户刚需：i18n 是 mod 放 mods 文件夹，解压即用）
    assert any(n.startswith("mods/") and n.endswith(".jar") for n in pk), "zip 应内置 i18n 汉化 mod"
    # 使用说明在 zip 内 + 产物区成品 zip 保留
    assert "使用说明.txt" in pk
    assert (out_dir / "整合包汉化.zip").exists()
    # 原整合包只读：目录文本源未被改写
    assert json.loads((tmp_path / "config/ftbquests/quests/1.json").read_text("utf-8"))["title"] == "Welcome"


@pytest.mark.asyncio
async def test_auto_vp_download_success(tmp_path, monkeypatch):
    """硬编码 + fabric 元数据：mock Modrinth 下载成功 → 补丁包含 VP jar + 映射模块，无 hardcoded jar。"""
    mods = tmp_path / "mods"
    mods.mkdir()
    with zipfile.ZipFile(mods / "meta.jar", "w") as zf:   # fabric 元数据 jar（提供 loader + MC 版本）
        zf.writestr("fabric.mod.json", json.dumps({"id": "meta", "depends": {"minecraft": ">=1.20.1"}}))
    _make_jar_with_hardcode(mods, name="h.jar")            # 硬编码 "Hello World"
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: _FakeEngine())

    async def fake_vp(loader, mc_version, client=None):
        return b"vpjarbytes"
    monkeypatch.setattr("app.auto_flow.download_vault_patcher", fake_vp)
    # 修复（recheck）：内置优先——mock 内置缺失强制走在线下载路径（本测试验证下载分支）
    monkeypatch.setattr("app.auto_flow.bundled_vp_jar", lambda: None)
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    assert store.load(state.id).status == "done"
    out_dir = outputs / state.id
    # VP 方案：vault-patcher.jar 在成品 zip 的 mods/（解压即用）+ 映射模块 vaultpatcher/ 目录
    pk = _pack_zip(out_dir)
    assert pk.get("mods/vault-patcher.jar") == b"vpjarbytes", "VP 下载成功应产出 vault-patcher.jar"
    pairs = _vp_pairs(out_dir)
    assert pairs.get("Hello World") == "你好世界"
    assert not (out_dir / "hardcoded").exists(), "VP 方案启用不产 hardcoded 汉化 jar"


@pytest.mark.asyncio
async def test_auto_vp_download_fallback_hardcoded_jar(tmp_path, monkeypatch):
    """硬编码 + 元数据但 mock 下载失败 → 回退 hardcoded 汉化 jar，补丁包不含 VP jar。"""
    mods = tmp_path / "mods"
    mods.mkdir()
    with zipfile.ZipFile(mods / "meta.jar", "w") as zf:
        zf.writestr("fabric.mod.json", json.dumps({"id": "meta", "depends": {"minecraft": ">=1.20.1"}}))
    _make_jar_with_hardcode(mods, name="h.jar")
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: _FakeEngine())

    async def fail_vp(loader, mc_version, client=None):
        return None
    monkeypatch.setattr("app.auto_flow.download_vault_patcher", fail_vp)
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    assert store.load(state.id).status == "done"
    out_dir = outputs / state.id
    # VP 获取失败：整合包不产 VP 映射，也不产修改版 jar（走 VP 形式生效，无二次分发纠纷）
    assert not (out_dir / "vaultpatcher/modules/mc-auto-translator.json").exists()
    assert not list((out_dir / "hardcoded").glob("*.jar"))
    patch = out_dir / "汉化补丁包.zip"
    if patch.exists():
        with zipfile.ZipFile(patch) as zf:
            assert "vault-patcher.jar" not in zf.namelist()



# ---------- 进度总量含硬编码候选（用户最新需求：语言文件 + 硬编码 一共数量） ----------

@pytest.mark.asyncio
async def test_auto_total_includes_hardcode_candidates(tmp_path, monkeypatch):
    """兜底引擎：进度总量 = 语言文件词条数 + 硬编码候选数（过滤 log 后），done 按候选数推进。"""
    mods = tmp_path / "mods"
    mods.mkdir()
    _make_mod_jar(mods)                            # 语言文件 1 词条（key.hello → Hello World）
    _make_jar_with_hardcode(mods, name="h.jar")    # 硬编码候选 "Hello World"
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: _FakeEngine())
    # 固定硬编码候选数（过滤 log 后的真实候选；兜底引擎走全翻）。
    # 只 h.jar 返回 2 条候选（_FakeEngine 都能翻译，避免 failed 干扰 total 断言）。
    monkeypatch.setattr(
        "app.auto_flow.scan_hardcoded_candidates",
        lambda jar: ([{"text": "Hello World", "occurrences": 1, "context": []},
                      {"text": "Welcome", "occurrences": 1, "context": []}]
                     if jar.name == "h.jar" else []))
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang="en_us")
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    st = store.load(state.id)
    assert st.status == "done"
    # total = 语言文件 1 + 硬编码候选 2 + build ≥1 单位（build 实际单位取决于
    # VP 下载成败/补丁包产出，收尾修正对账 → done 恒到 total）
    assert st.total >= 1 + 2 + 1
    # done 按候选数推进：语言文件 1 + 硬编码 2 + build 单位都算已处理，且推进到顶
    assert st.done == st.total
    assert st.done <= st.total
    # 阶段结构：lang 1 + hardcode 2，各阶段 done 对齐，build done 到顶
    stages = {s["name"]: s for s in st.stages}
    assert stages["lang"]["total"] == 1 and stages["lang"]["done"] == 1
    assert stages["hardcode"]["total"] == 2 and stages["hardcode"]["done"] == 2
    assert stages["build"]["done"] == stages["build"]["total"]


@pytest.mark.asyncio
async def test_auto_llm_total_includes_all_candidates(tmp_path, monkeypatch):
    """LLM 引擎：进度总量含全部硬编码候选（非只含 AI 判定可见），done 按候选数推进。"""
    from app.translate.llm import LLMClient

    mods = tmp_path / "mods"
    mods.mkdir()
    _make_jar_with_hardcode(mods, name="h.jar")   # 硬编码候选 "Hello World"（无语言文件）
    engine = LLMClient("https://x", "k", "m")
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: engine)

    async def fake_judge(engine, candidates, target, known_translations=None,
                         on_batch_done=None, on_batch_start=None,
                         silly_mode=False):
        if on_batch_start:
            on_batch_start(len(candidates))
        if on_batch_done:
            on_batch_done(len(candidates))
        return {"Hello World": "你好世界"}        # AI 判定 1 条可见

    monkeypatch.setattr("app.auto_flow.ai_judge_translate", fake_judge)
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang="en_us")
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    st = store.load(state.id)
    assert st.status == "done"
    # total = 硬编码候选数（无语言文件/文本源）+ build 2 单位（hardcoded jar + 整合包汉化.zip）
    assert st.total == 1 + 2
    # done 按候选数推进（AI 判定可见的 1 条）+ build 2
    assert st.done == 1 + 2
    assert st.done <= st.total
    # 阶段结构：hardcode 1 + build 2
    stages = {s["name"]: s for s in st.stages}
    assert stages["hardcode"]["total"] == 1 and stages["hardcode"]["done"] == 1
    assert stages["build"]["done"] == stages["build"]["total"]
    # 批前反馈（on_batch_start）先于 done 跳变：translating 标记的 note 提示 AI 判断硬编码
    trans_items = [p for p in st.progress if p.get("status") == "translating"]
    assert trans_items, "硬编码批请求前应有「AI 判断硬编码」反馈"
    assert any(p.get("note") == "AI 判断硬编码" for p in trans_items)


@pytest.mark.asyncio
async def test_auto_cfpa_auto_download_and_hit(tmp_path, monkeypatch):
    """词库自动下载 + 语言文件命中：检测版本 → 自动下载词库 → 命中 key 直接写回不走引擎。

    cfpa_path 传入时按检测的 MC 版本自动下载；语言文件阶段按 (modid, key) 命中词库
    直接写回（不走引擎），并给出「已下载」进度提示。
    """
    from app.cfpa import load_cfpa, match_zip_name, save_cfpa

    mods = tmp_path / "mods"
    mods.mkdir()
    _make_mod_jar(mods)                                   # assets/mymod/lang/en_us: key.hello → Hello World
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: _FakeEngine())

    cfpa_path = tmp_path / "cfpa_glossary.json"

    async def fake_download(mc_ver, target, client=None):
        g = {"by_key": {"mymod\x00key.hello": "你好"}, "count": 1,
             "mc_version": match_zip_name(mc_ver), "size_mb": 0.0}
        save_cfpa(g, target)
        return load_cfpa(target)

    monkeypatch.setattr("app.auto_flow.download_cfpa", fake_download)
    monkeypatch.setattr("app.auto_flow._detect_mc_version", lambda kind, path, jars: "1.20.1")
    # 模拟无内置汉化包（新逻辑内置优先）→ 走在线下载路径验证下载+命中
    monkeypatch.setattr("app.auto_flow.load_bundled_cfpa", lambda mc_ver: None)

    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang="en_us")
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs, cfpa_path=cfpa_path)
    st = store.load(state.id)
    assert st.status == "done"
    # 词库下载/就绪进度提示
    assert any(p.get("key") == "社区词库" for p in st.progress), "应有词库进度提示"
    # 语言文件命中词库：资源包 zh_cn 里 key.hello = 你好（直接写回，不走引擎）
    data = json.loads(_pack_zip(outputs / state.id)["resourcepacks/模组汉化资源包/assets/mymod/lang/zh_cn.json"])
    assert data["key.hello"] == "你好"
    # 词库文件已落盘到 cfpa_path
    assert load_cfpa(cfpa_path)["by_key"].get("mymod\x00key.hello") == "你好"


@pytest.mark.asyncio
async def test_auto_fatal_error_short_circuit(tmp_path, monkeypatch):
    """致命错误（API key 无效）立即失败，不逐条 × 4 空转重试（「卡半天」根因）。"""
    mods = tmp_path / "mods"
    mods.mkdir()
    with zipfile.ZipFile(mods / "m.jar", "w") as zf:
        zf.writestr("assets/mymod/lang/en_us.json", json.dumps({"k": "Hello World"}))
    calls = [0]

    class _FatalEngine:
        batch_size = 10
        _fatal_error = "API Key 无效或无权限（HTTP 401）"
        _batch_failed_texts = set()
        _last_error_kind = "auth"

        async def translate_batch(self, texts, target_lang):
            calls[0] += 1
            raise ValueError(self._fatal_error)

    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: _FatalEngine())
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    st = store.load(state.id)
    assert st.status == "failed"
    # 致命错误短路：名字翻译 1 次 + 语言文件主请求 1 次抛异常 → 直接失败，不逐条空转
    assert calls[0] <= 3, f"致命错误不应逐条重试，实际 {calls[0]} 次"


@pytest.mark.asyncio
async def test_auto_network_timeout_recovers(tmp_path, monkeypatch):
    """网络超时：等待网络恢复后重试成功，不记 failed（用户诉求——网络不好不跳过翻译）。"""
    mods = tmp_path / "mods"
    mods.mkdir()
    with zipfile.ZipFile(mods / "m.jar", "w") as zf:
        zf.writestr("assets/mymod/lang/en_us.json", json.dumps({"k": "Hello World"}))
    calls = [0]

    class _NetEngine:
        batch_size = 10
        _fatal_error = None
        _batch_failed_texts = set()
        _last_error_kind = "other"

        async def translate_batch(self, texts, target_lang):
            calls[0] += 1
            if calls[0] <= 2:   # 前两次：网络超时失败（名字翻译 + 语言文件主请求）
                self._batch_failed_texts.update(texts)
                self._last_error_kind = "timeout"
                return list(texts)
            self._batch_failed_texts.clear()   # 网络恢复：正常翻译
            self._last_error_kind = "other"
            return [t.replace("Hello", "你好") for t in texts]

    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: _NetEngine())
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    st = store.load(state.id)
    assert st.status == "done"
    assert st.failed == 0, f"网络恢复后不应记失败：{st.failed}"


# ---------- 术语一致性归一化（Zeno→泽诺/泽昂/zeno 三样并存 → 统一为最高频） ----------

def _make_flow(tmp_path, task_id="t1"):
    """构造最小 AutoFlow 实例（只跑归一化/审计相关方法，不跑完整 run）。"""
    from app.auto_flow import AutoFlow
    from app.tasks import TaskState, TaskStore

    store = TaskStore(tmp_path / "tasks")
    store.save(TaskState(id=task_id))
    req = SimpleNamespace(target_lang="zh_cn", source_lang="en_us",
                          path=str(tmp_path / "pack.zip"))
    return AutoFlow(task_id, req, None, store, tmp_path / "work", tmp_path / "out", None)


# v1.1.0：test_consistency_normalize_* 已删除（机械归一化移除，语义由
# _ai_contextual_normalize / _collect_norm_candidates 测试覆盖）


@pytest.mark.asyncio
async def test_prebuild_terms_strips_particle_suffix(tmp_path, monkeypatch):
    """预扫描单词术语译名以格助词结尾（Rune→符文的）→ 剥离为「符文」再登记。
    修复用户实测「Reinforced Rune of the Orb→强化符文的的宝珠」：带「的」的单词译名
    作词级术语注入 glossary，会让 AI 在「X of the Y」结构里再叠一个「的」→「的的」。
    剥离后为空/单字丢弃（词级术语须有独立名词义）；保留原文不登记。"""
    from app.auto_flow import AutoFlow
    flow = _make_flow(tmp_path)
    flow.engine = SimpleNamespace(glossary_prompt="")   # engine 原为 None，让 glossary 注入可赋值

    async def fake_engine_translate(texts, reasons=None, **kw):
        table = {"Rune": "符文的", "Orb": "宝珠的", "Reinforced": "强化",
                 "Power": "Power", "Light": "Light", "Blade": "Blade"}
        return [table.get(t, t) for t in texts], {}

    monkeypatch.setattr(flow, "_engine_translate", fake_engine_translate)
    # Rune/Orb/Reinforced 各 ≥3 次触发预扫描；Power 等保留原文 → 不登记
    texts = (["Rune of Power"] * 3 + ["Orb of Light"] * 3 + ["Reinforced Blade"] * 3)
    await flow._prebuild_terms(texts)
    assert flow._norm_terms.get("Rune") == "符文"         # 助词「的」被剥离
    assert flow._norm_terms.get("Orb") == "宝珠"
    assert flow._norm_terms.get("Reinforced") == "强化"   # 无助词原样
    assert "符文的" not in flow._norm_terms.values()      # 不存在带「的」的单词译名
    assert "Power" not in flow._norm_terms                # 保留原文不登记
    assert "符文" in flow.glossary_prompt                 # glossary 注入剥离后译名


def test_is_roman_valid():
    """合法罗马数字判定（修复 Agent 审查：原字符集校验把 DVD/CIVIL 等英文词误判保留）。"""
    from app.auto_flow import _is_roman
    assert _is_roman("I") and _is_roman("IV") and _is_roman("IX")
    assert _is_roman("X") and _is_roman("XL") and _is_roman("XC")
    assert _is_roman("CM") and _is_roman("MCMXCIX") and _is_roman("VIII")
    assert not _is_roman("DVD")       # 真实英文词，不再误判为罗马数字
    assert not _is_roman("CIVIL")
    assert not _is_roman("LCD")
    assert not _is_roman("")          # 空串
    assert not _is_roman("IIII")      # 4 个 I 非法（应为 IV）
    assert not _is_roman("VX")        # 非法规组合


def test_is_proper_noun():
    """专名形态筛选（AI 语境归一化候选门槛）：专名进候选，常用词（light/right）绝不进。"""
    from app.auto_flow import _is_proper_noun
    # 专名/特有名词形态 → 进候选
    assert _is_proper_noun("Zeno")
    assert _is_proper_noun("Diamond Sword")
    assert _is_proper_noun("ZenoSword")
    assert _is_proper_noun("Iron Ingot")
    assert _is_proper_noun("No_Minimap")
    assert _is_proper_noun("Craft-Table")
    # 常用词/小写短语 → 绝不进（用户核心诉求：right 不能全替换成右面）
    assert not _is_proper_noun("light")
    assert not _is_proper_noun("right")
    assert not _is_proper_noun("iron")
    assert not _is_proper_noun("stone")
    assert not _is_proper_noun("of the orb")
    assert not _is_proper_noun("click")
    # 边界
    assert not _is_proper_noun("")
    assert not _is_proper_noun("a")
    assert not _is_proper_noun("123")


# ---------- 名称归一化（第一定义 + 后续跟随，审查通过的关键步骤） ----------

def test_name_norm_first_define_and_follow(tmp_path):
    """第一个翻出对应语言的译名登记为规范译名，后续不一致译文在审查时归一化覆盖。"""
    flow = _make_flow(tmp_path)
    # 第一个 Zeno 审查通过翻出中文 → 登记规范译名
    sink1 = {}
    flow._write_reviewed({"key": "k1", "source": "Zeno", "modid": "m", "sink": sink1}, "泽诺")
    assert flow._norm_terms["Zeno"] == "泽诺"
    assert sink1["k1"] == "泽诺"
    # 后续同一原文被 AI 翻成泽昂 → v1.1.0 **不机械覆盖**（保留语境译文；多译文冲突
    # 由 AI 语境归一化审查 _ai_contextual_normalize 判定语境后统一，而非强制替换）
    sink2 = {}
    flow._write_reviewed({"key": "k2", "source": "Zeno", "modid": "m", "sink": sink2}, "泽昂")
    assert sink2["k2"] == "泽昂"                     # 不被机械覆盖
    assert flow._norm_terms["Zeno"] == "泽诺"         # 规范译名仍为第一个登记


def test_name_norm_keep_original_not_registered(tmp_path):
    """AI 保留原文（非目标语言）不登记规范译名；后续翻出目标语言才登记。"""
    flow = _make_flow(tmp_path)
    sink = {}
    flow._write_reviewed({"key": "k1", "source": "Youtube", "modid": "m", "sink": sink}, "Youtube")
    assert "Youtube" not in flow._norm_terms       # 保留原文不登记
    assert sink["k1"] == "Youtube"                 # 合理保留，产物原文
    flow._write_reviewed({"key": "k2", "source": "Youtube", "modid": "m", "sink": sink}, "油管")
    assert flow._norm_terms["Youtube"] == "油管"    # 翻出目标语言才登记


def test_name_norm_register_requires_target_lang(tmp_path):
    """规范译名登记前提：译文必须是对应语言（zh_cn 需含中文），英文译文不登记。"""
    flow = _make_flow(tmp_path)
    flow._write_reviewed({"key": "k1", "source": "Zeno", "modid": "m", "sink": {}}, "Zeno's Power")
    assert "Zeno" not in flow._norm_terms          # 英文译文不登记
    flow._write_reviewed({"key": "k2", "source": "Zeno", "modid": "m", "sink": {}}, "泽诺")
    assert flow._norm_terms["Zeno"] == "泽诺"       # 中文译文登记


# ---------- 名称归一化修复验证（recheck 回归） ----------

def test_apply_name_norm_proper_noun_only(tmp_path):
    """v1.1.0：只对**专名形态**原文登记规范译名（Youtube 大写→登记「油管」）；
    小写常用词（light）绝不登记、绝不干预（用户核心诉求：不用机械统一破坏语境）。"""
    flow = _make_flow(tmp_path)
    # 专名 → 登记规范译名，但不强制覆盖（原样返回）
    assert flow._apply_name_norm("Youtube", "油管") == "油管"
    assert flow._norm_terms["Youtube"] == "油管"
    # 非专名（light 小写）→ 不登记、不干预
    flow2 = _make_flow(tmp_path, task_id="t2")
    assert flow2._apply_name_norm("light", "灯") == "灯"
    assert "light" not in flow2._norm_terms
    # 保留原文（translated==src）不登记
    flow3 = _make_flow(tmp_path, task_id="t3")
    assert flow3._apply_name_norm("Zeno", "Zeno") == "Zeno"
    assert "Zeno" not in flow3._norm_terms


# ---------- 合理保留分流收窄（recheck：该翻的不能误判保留） ----------

def test_legit_keep_by_source_narrowed():
    """只预保留「确定翻不动」的（无字母/代码标识），该翻的真文本一律送 AI 审查。"""
    from app.auto_flow import _is_legit_keep_by_source
    # 该翻的界面文本 → 不应预判保留（送 AI 审查判定）
    for t in ["Left (click)", "Press [E] to open inventory", "(Requires level 30)",
              "Invite", "Connect", "Settings", "Block Reach", "World Map",
              "Raining/Snowing", "Open Chat", "Player List"]:
        assert _is_legit_keep_by_source(t) is False, f"该翻的却判保留: {t}"
    # 确定翻不动的 → 预保留
    for t in ["(%1$s): %2$s", "§e>§r %s §e<§r", "com.example.Mod",
              "%s", "§9+1.5"]:
        assert _is_legit_keep_by_source(t) is True, f"该保留的却送审查: {t}"
    # 小写路径（无 .: 分隔）→ 送 AI 判定（AI 识别为路径保留）；Raining/Snowing 类送 AI 翻
    assert _is_legit_keep_by_source("path/to/thing") is False


# ---------- 光影产物名（修复：哈希 → 原光影名-对应语言化） ----------

@pytest.mark.asyncio
async def test_stage_shader_output_name_uses_original(tmp_path, monkeypatch):
    """光影产物名用**原光影名**而非解压缓存目录的哈希指纹：
    产物 zip 名 = {原光影名}-{语言}化.zip（用户实测光影产物名是哈希值）。"""
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: _FakeEngine())
    flow = _make_flow(tmp_path)
    flow.state.status = "running"
    flow.store.save(flow.state)
    # 模拟解压缓存：目录名是哈希指纹（self.path.name 会是 5a818a7428e7），内含光影语言文件
    shader_pack = tmp_path / "work" / "extracted" / "5a818a7428e7"
    (shader_pack / "shaders" / "lang").mkdir(parents=True)
    (shader_pack / "shaders" / "lang" / "en_US.lang").write_text(
        "title=Hello World\n", encoding="utf-8")
    flow.path = shader_pack
    # run() 开头已把原始输入名写入 display_name（本次修复）：原光影名，去扩展名
    flow.state.display_name = "SEUS PTGI HRR3"
    # 翻译流水线用假引擎直填译文（绕开批处理细节，专注产物名）。
    # translate_fn 现为 _engine_translate（返回 (results, meta) 元组，同真实 pipeline 约定）
    async def fake_pipeline(items, fn, batch_size, skip_fn=None):
        for item in items:
            r = await fn([item["text"]])
            res = r[0] if (isinstance(r, tuple) and len(r) == 2
                           and isinstance(r[1], dict)) else r
            item["sink"][item["key"]] = res[0]
    flow._translate_batch_pipeline = fake_pipeline
    await flow._stage_shader()
    assert flow.state.status == "done"
    out_dir = flow.outputs_dir / flow.task_id
    zips = list(out_dir.glob("*.zip"))
    assert len(zips) == 1
    assert zips[0].name == "SEUS_PTGI_HRR3-简体中文化.zip", \
        f"光影产物名应为原光影名，实际: {zips[0].name}"
    # 译文写进 zh_CN.lang
    zh = (out_dir / "SEUS_PTGI_HRR3" / "shaders" / "lang" / "zh_CN.lang").read_text(encoding="utf-8")
    assert "title=你好世界" in zh


@pytest.mark.asyncio
async def test_auto_modpack_pack_format_from_mc_version(tmp_path, monkeypatch):
    """整合包 MC 版本自动识别 → pack.mcmeta 写对应 pack_format（端到端验证版本表修复：
    1.20.6 → 32；之前表整体错位会把产物写成错误格式，对应版本游戏拒载「材质包不兼容」）。"""
    mods = tmp_path / "mods"
    mods.mkdir()
    jar = mods / "mod.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("assets/mymod/lang/en_us.json", json.dumps({"key.hello": "Hello World"}))
        # fabric.mod.json 声明依赖 MC 1.20.6 → detect_mc_version 应识别 → pack_format 32
        zf.writestr("fabric.mod.json", json.dumps({"schemaVersion": 1,
                                                   "depends": {"minecraft": "1.20.6"}}))
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: _FakeEngine())
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, {"force_hardcode": True}, store, work, outputs)
    assert store.load(state.id).status == "done"
    pk = _pack_zip(outputs / state.id)
    meta = json.loads(pk["resourcepacks/模组汉化资源包/pack.mcmeta"])
    assert meta["pack"]["pack_format"] == 32, f"1.20.6 应写 pack_format 32，实际 {meta['pack']}"


def test_no_mechanical_protect_for_common_words(tmp_path):
    """v1.1.0：机械术语保护/词级覆盖（_protect_terms/_apply_term_override）已删除——
    不再对任何词做占位符机械替换（right→右面 全替换的元凶）；归一化只靠
    _is_proper_noun 形态筛选进候选 + AI 语境判定（_ai_contextual_normalize）。"""
    from app.auto_flow import _is_proper_noun
    flow = _make_flow(tmp_path)
    # 机械函数已删除
    assert not hasattr(flow, "_protect_terms")
    assert not hasattr(flow, "_restore_terms")
    assert not hasattr(flow, "_apply_term_override")
    # 组合词形态：首字母大写 → 进归一化候选（AI 判定语境）；小写常用词 → 绝不进
    assert _is_proper_noun("Zeno Red Armor")
    assert _is_proper_noun("Zeno's Sword")
    assert not _is_proper_noun("light blue")
    assert not _is_proper_noun("right click")


# ---------- AI 语境归一化（v1.1.0 重构：替代机械 _consistency_normalize） ----------

def test_collect_norm_candidates_only_proper_noun_conflicts(tmp_path):
    """候选收集：只收「同原文 ≥2 个译文」且**专名形态**；单译名/小写常用词不触发。"""
    flow = _make_flow(tmp_path)
    flow._record_consistency("Zeno", "泽诺")
    flow._record_consistency("Zeno", "泽昂")       # 专名多译文 → 进候选
    flow._record_consistency("light", "灯")
    flow._record_consistency("light", "亮")         # 小写常用词多译文 → 绝不进
    flow._record_consistency("Iron Ingot", "铁锭")  # 单译名 → 不触发
    cands = flow._collect_norm_candidates()
    assert len(cands) == 1
    assert cands[0]["source"] == "Zeno"


@pytest.mark.asyncio
async def test_ai_contextual_normalize_flow(tmp_path, monkeypatch):
    """AI 语境归一化流程：只对 Zeno（专名多译文）判定统一；light 常用词不触发。"""
    flow = _make_flow(tmp_path)
    flow._record_consistency("Zeno", "泽诺")
    flow._record_consistency("Zeno", "泽昂")
    flow._record_consistency("light", "灯")
    flow._record_consistency("light", "亮")

    judged_calls, renormalized = [], []

    async def fake_judge(cands):
        judged_calls.append(cands)
        return {0: {"should_unify": True, "canonical": "泽诺"}}

    async def fake_renormalize(cands, judged):
        renormalized.append((cands, judged))

    monkeypatch.setattr(flow, "_ai_judge_normalization", fake_judge)
    monkeypatch.setattr(flow, "_ai_renormalize", fake_renormalize)
    await flow._ai_contextual_normalize()
    assert len(judged_calls) == 1 and judged_calls[0][0]["source"] == "Zeno"
    assert len(renormalized) == 1


@pytest.mark.asyncio
async def test_ai_renormalize_only_proper_noun_unify(tmp_path, monkeypatch):
    """归一化重翻：只对判定统一的专名条目重翻；已是规范译名/非候选不重翻。"""
    flow = _make_flow(tmp_path)
    flow.by_mod["m"] = {"k1": "泽诺", "k2": "泽昂", "k3": "灯"}
    flow.source_by_mod["m"] = {"k1": "Zeno", "k2": "Zeno", "k3": "light"}
    cands = [{"source": "Zeno", "variants": ["泽诺", "泽昂"]}]
    judged = {0: {"should_unify": True, "canonical": "泽诺"}}

    collected = []

    async def fake_pipeline(items, fn, batch_size=5, **kw):
        collected.extend(items)

    monkeypatch.setattr(flow, "_translate_batch_pipeline", fake_pipeline)
    await flow._ai_renormalize(cands, judged)
    keys = [i["key"] for i in collected]
    assert "k2" in keys            # Zeno 的「泽昂」≠ 规范译名 → 重翻
    assert "k1" not in keys        # 已是「泽诺」→ 不重翻
    assert "k3" not in keys        # light 非候选 → 不重翻


def test_scan_hardcode_all_method_present():
    """v1.2.7 轻量化：硬编码扫描抽为独立方法 _scan_hardcode_all（供 run 与语言翻译并行）。"""
    from app.auto_flow import AutoFlow
    assert callable(getattr(AutoFlow, "_scan_hardcode_all", None))
