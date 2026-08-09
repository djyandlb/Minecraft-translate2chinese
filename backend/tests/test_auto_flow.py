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
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, None, store, work)
    assert store.load(state.id).status == "done"
    hards = list((work / "outputs" / state.id / "hardcoded").glob("*.jar"))
    assert len(hards) == 2, f"期望 2 个汉化 jar，实际 {hards}"
    assert len({h.name for h in hards}) == 2  # 文件名互不相同，未互相覆盖


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
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, None, store, work)
    assert store.load(state.id).status == "done"
    out_dir = work / "outputs" / state.id
    # 无可导出产物：无资源包 zip，hardcoded 目录不存在或为空
    assert not list(out_dir.glob("*.zip"))
    hard_dir = out_dir / "hardcoded"
    assert not hard_dir.exists() or not list(hard_dir.glob("*.jar"))


@pytest.mark.asyncio
async def test_auto_engine_exception_does_not_kill_flow(tmp_path, monkeypatch):
    """M6-recheck：单条引擎异常（网络/API 失败）→ 记 failed 继续，流程不整体失败。"""
    class _ExplodingEngine:
        async def translate_batch(self, texts, target_lang):
            raise RuntimeError("API 失败")
    mods = tmp_path / "mods"
    mods.mkdir()
    _make_mod_jar(mods)  # 语言文件 mod（含 "Hello World"）
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: _ExplodingEngine())
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, None, store, work)
    st = store.load(state.id)
    assert st.status == "done"   # 单条失败不拖垮整体流程
    assert st.failed >= 1        # 失败已计数


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
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None,
                          selected_hardcoded=["Hello World"])
    await run_auto_translation(state.id, req, None, store, work)
    assert store.load(state.id).status == "done"
    hards = list((work / "outputs" / state.id / "hardcoded").glob("*.jar"))
    assert hards, "应产出汉化 jar"
    found = scan_hardcoded_strings(hards[0])
    assert "你好世界" in found            # "Hello World" 已翻译替换
    assert "Hello World" not in found
    assert "欢迎" in found                 # "Welcome" 也被翻译（selected_hardcoded 不再生效）
    assert "Welcome" not in found


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
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, None, store, work)
    st = store.load(state.id)
    assert st.status == "done"
    # 语言文件正常翻 → 资源包产出
    packs = list((work / "outputs" / state.id).glob("*_zh_cn.zip"))
    assert packs, "语言文件应正常翻译成资源包"
    with zipfile.ZipFile(packs[0]) as zf:
        data = json.loads(zf.read("assets/mymod/lang/zh_cn.json").decode("utf-8"))
    assert data["key.hello"] == "你好世界"
    # 硬编码跳过：无 hardcoded jar 产物 + warn 明确提示
    hard_dir = work / "outputs" / state.id / "hardcoded"
    assert not hard_dir.exists() or not list(hard_dir.glob("*.jar"))
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
        zf.writestr("assets/mymod/advancement.json", json.dumps({"title": {"text": "Welcome"}}))
        zf.writestr("assets/mymod/patchouli_books/guide/en_us/entries/intro.md",
                    "# Intro\r\nWelcome to the mod\r\n")
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: _JsonLinesEngine())
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, None, store, work)
    assert store.load(state.id).status == "done"
    # 汉化 jar 产物（json/lines 写回 jar 副本）
    hards = list((work / "outputs" / state.id / "hardcoded").glob("*.jar"))
    assert hards, "json/lines 写回应产出汉化 jar"
    with zipfile.ZipFile(hards[0]) as zf:
        data = json.loads(zf.read("assets/mymod/advancement.json").decode("utf-8"))
        md = zf.read("assets/mymod/patchouli_books/guide/zh_cn/entries/intro.md").decode("utf-8")
    assert data["title"]["text"] == "欢迎"       # 结构化 json 译文写回
    assert "欢迎来到本模组" in md                 # lines 译文写回 zh_cn 路径
    assert "# Intro" in md                       # 未翻译行保留


@pytest.mark.asyncio
async def test_auto_llm_ai_judge_only_visible(tmp_path, monkeypatch):
    """LLM 引擎：ai_judge_translate 只替换 AI 判定可见的硬编码（mock ai_judge）。"""
    from app.translate.llm import LLMClient

    mods = tmp_path / "mods"
    mods.mkdir()
    _make_jar_with_two_hardcode(mods, name="two.jar")   # "Hello World" + "Welcome"
    engine = LLMClient("https://x", "k", "m")
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: engine)

    async def fake_judge(engine, candidates, target):
        return {"Hello World": "你好世界"}   # AI 只判定 Hello World 可见

    monkeypatch.setattr("app.auto_flow.ai_judge_translate", fake_judge)
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, None, store, work)
    st = store.load(state.id)
    assert st.status == "done"
    hards = list((work / "outputs" / state.id / "hardcoded").glob("*.jar"))
    assert hards, "LLM AI 判断应产出汉化 jar"
    found = scan_hardcoded_strings(hards[0])
    assert "你好世界" in found                # AI 判定可见 → 替换
    assert "Hello World" not in found
    assert "Welcome" in found                 # AI 判定不可见 → 保持原文
    assert "欢迎" not in found
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

    async def boom_judge(engine, candidates, target):
        raise RuntimeError("LLM 服务不可用")

    monkeypatch.setattr("app.auto_flow.ai_judge_translate", boom_judge)
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, None, store, work)
    st = store.load(state.id)
    assert st.status == "done"
    assert st.failed >= 1                       # 异常整批计入 failed
    assert st.done + st.failed <= st.total      # 不双计：done+failed 不超 total
    hard_dir = work / "outputs" / state.id / "hardcoded"
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

    async def judge_then_cancel(engine, candidates, target):
        calls["n"] += 1
        # 处理完第一个 jar 后立即置取消（TaskStore 缓存使 auto_flow 与本测试共享同一 state 对象）
        if calls["n"] == 1:
            state.cancelled = True
            store.save(state)
        return {"Hello World": "你好世界"}

    monkeypatch.setattr("app.auto_flow.ai_judge_translate", judge_then_cancel)
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, None, store, work)
    st = store.load(state.id)
    assert st.status == "cancelled"
    assert calls["n"] == 1        # 第二个 jar 前被取消拦截，不再调用 ai_judge
