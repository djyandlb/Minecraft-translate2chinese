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
    await run_auto_translation(state.id, req, None, store, work, outputs)
    assert store.load(state.id).status == "done"
    # 产物：资源包 + hardcoded jar
    out_dir = outputs / state.id
    packs = list(out_dir.glob("模组汉化资源包.zip"))
    hards = list((out_dir / "hardcoded").glob("*.jar"))
    assert packs and hards, f"产物缺失 packs={packs} hards={hards}"
    # 汉化 jar 内字符串已被替换（副本被改，原 jar 只读）
    assert "你好世界" in " ".join(scan_hardcoded_strings(hards[0]))
    # 资源包内含汉化词条
    with zipfile.ZipFile(packs[0]) as zf:
        data = json.loads(zf.read("assets/mymod/lang/zh_cn.json").decode("utf-8"))
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
    await run_auto_translation(state.id, req, None, store, work, outputs)
    assert store.load(state.id).status == "done"
    packs = list((outputs / state.id).glob("模组汉化资源包.zip"))
    assert packs
    with zipfile.ZipFile(packs[0]) as zf:
        data = json.loads(zf.read("assets/mymod/lang/zh_cn.json").decode("utf-8"))
    assert data["item.armor"] == "需要盔甲"


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
    await run_auto_translation(state.id, req, None, store, work, outputs)
    assert store.load(state.id).status == "done"
    packs = list((outputs / state.id).glob("模组汉化资源包.zip"))
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
    await run_auto_translation(state.id, req, None, store, work, outputs)
    assert called and called["req"].path == str(w)
    assert called["outputs_dir"] == outputs   # map 委托也透传产物目录


def test_download_packs_output_dir(tmp_path, monkeypatch):
    """download：产物目录存在时打包总 zip（含 hardcoded jar），旧单文件兼容兜底。"""
    import app.main as main
    from fastapi.testclient import TestClient

    out_dir = tmp_path / "outputs" / "abc123def456"
    out_dir.mkdir(parents=True)
    (out_dir / "模组汉化资源包.zip").write_bytes(b"packdata")
    hard = out_dir / "hardcoded"
    hard.mkdir()
    (hard / "abc123def456_h.jar").write_bytes(b"jardata")
    monkeypatch.setattr(main, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(main, "OUTPUTS_DIR", tmp_path / "outputs")
    client = TestClient(main.app)
    r = client.get("/api/task/abc123def456/download")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = zf.namelist()
    assert "模组汉化资源包.zip" in names
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
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, None, store, work, outputs)
    assert store.load(state.id).status == "done"
    hards = list((outputs / state.id / "hardcoded").glob("*.jar"))
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
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, None, store, work, outputs)
    assert store.load(state.id).status == "done"
    out_dir = outputs / state.id
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
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, None, store, work, outputs)
    st = store.load(state.id)
    assert st.status == "done"   # 单条失败不拖垮整体流程
    assert st.failed >= 1        # 失败已计数


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
    await run_auto_translation(state.id, req, None, store, work, outputs)
    assert store.load(state.id).status == "done"
    hards = list((outputs / state.id / "hardcoded").glob("*.jar"))
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
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, None, store, work, outputs)
    st = store.load(state.id)
    assert st.status == "done"
    # machine 引擎 total 不含硬编码候选（只含语言文件 1 词条）
    assert st.total == 1
    # 语言文件正常翻 → 资源包产出
    packs = list((outputs / state.id).glob("模组汉化资源包.zip"))
    assert packs, "语言文件应正常翻译成资源包"
    with zipfile.ZipFile(packs[0]) as zf:
        data = json.loads(zf.read("assets/mymod/lang/zh_cn.json").decode("utf-8"))
    assert data["key.hello"] == "你好世界"
    # 硬编码跳过：无 hardcoded jar 产物 + warn 明确提示
    hard_dir = outputs / state.id / "hardcoded"
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
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, None, store, work, outputs)
    assert store.load(state.id).status == "done"
    # 汉化 jar 产物（json/lines 写回 jar 副本）
    hards = list((outputs / state.id / "hardcoded").glob("*.jar"))
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

    async def fake_judge(engine, candidates, target, on_batch_done=None):
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
    await run_auto_translation(state.id, req, None, store, work, outputs)
    st = store.load(state.id)
    assert st.status == "done"
    hards = list((outputs / state.id / "hardcoded").glob("*.jar"))
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
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(tmp_path), target_lang="zh_cn", source_lang=None)
    await run_auto_translation(state.id, req, None, store, work, outputs)
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

    async def judge_then_cancel(engine, candidates, target, on_batch_done=None):
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
    await run_auto_translation(state.id, req, None, store, work, outputs)
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
    # 中间产物已清理
    for sub in ("jars", "extracted", "maps", "uploads"):
        assert not (work / sub / state.id).exists(), f"{sub} 任务后未清理"
    # 产物保留在 OUTPUTS_DIR
    packs = list((outputs / state.id).glob("模组汉化资源包.zip"))
    assert packs, "产物应保留在 outputs"


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
    await run_auto_translation(state.id, req, None, store, work, outputs)
    assert store.load(state.id).status == "done"
    out_dir = outputs / state.id
    packs = list(out_dir.glob("模组汉化资源包.zip"))
    hards = list((out_dir / "hardcoded").glob("*.jar"))
    assert packs and hards, f"modpack 应产出资源包 zip + hardcoded jar，实际 packs={packs} hards={hards}"
    # 顶层无单 jar（汉化 jar 全在 hardcoded/ 子目录）
    assert not list(out_dir.glob("*.jar"))
    with zipfile.ZipFile(packs[0]) as zf:
        data = json.loads(zf.read("assets/mymod/lang/zh_cn.json").decode("utf-8"))
    assert data["key.hello"] == "你好世界"


def test_lang_display_name_mapping():
    """汉化命名映射：zh_cn→简体中文、zh_tw→繁体中文，其他 target_lang 原样。"""
    from app.auto_flow import lang_display_name
    assert lang_display_name("zh_cn") == "简体中文"
    assert lang_display_name("zh_tw") == "繁体中文"
    assert lang_display_name("ja_jp") == "ja_jp"
    assert lang_display_name("fr_fr") == "fr_fr"


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
    # 批量断言：ceil(5/2)=3 次调用；每次 1-2 条；至少一次 >1 条；总量 = 5
    assert len(spy.calls) == 3, f"期望 3 次批量调用，实际 {len(spy.calls)} 次: {spy.calls}"
    assert all(0 < len(c) <= 2 for c in spy.calls)
    assert any(len(c) > 1 for c in spy.calls), "应至少有一次调用传入多条文本"
    assert sum(len(c) for c in spy.calls) == 5
    # 译文逐条写回产物（批量后产物/进度仍正确）
    packs = list((outputs / state.id).glob("模组汉化资源包.zip"))
    assert packs
    with zipfile.ZipFile(packs[0]) as zf:
        data = json.loads(zf.read("assets/mymod/lang/zh_cn.json").decode("utf-8"))
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
    (tmp_path / "data/demo/advancement").mkdir(parents=True)
    (tmp_path / "data/demo/advancement/t.json").write_text(
        json.dumps({"title": {"text": "New World"}}), encoding="utf-8")
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
    # 两类产物：模组汉化资源包 + 汉化补丁包
    assert (out_dir / "模组汉化资源包.zip").exists()
    assert (out_dir / "汉化补丁包.zip").exists()
    with zipfile.ZipFile(out_dir / "模组汉化资源包.zip") as zf:
        data = json.loads(zf.read("assets/mymod/lang/zh_cn.json").decode("utf-8"))
    assert data["key.hello"] == "你好世界"
    # 补丁包：使用说明 + 相对路径条目 + 译文/技术串正确
    with zipfile.ZipFile(out_dir / "汉化补丁包.zip") as zf:
        names = zf.namelist()
        assert "使用说明.txt" in names
        assert "config/ftbquests/quests/1.json" in names
        assert "data/demo/advancement/t.json" in names
        assert "kubejs/server_scripts/main.js" in names
        quest = json.loads(zf.read("config/ftbquests/quests/1.json").decode("utf-8"))
        js = zf.read("kubejs/server_scripts/main.js").decode("utf-8")
    assert quest["title"] == "欢迎"               # 任务线译文
    assert quest["item"] == "minecraft:stone"      # 技术串原样保留
    assert "你好世界" in js                         # kubejs 字符串字面量译文
    # 解压到临时整合包根目录 → 文件精确落在整合包对应位置（路径对齐，无需手动移动）
    extract_root = tmp_path / "extract_root"
    extract_root.mkdir()
    with zipfile.ZipFile(out_dir / "汉化补丁包.zip") as zf:
        zf.extractall(extract_root)
    assert (extract_root / "使用说明.txt").exists()
    assert json.loads((extract_root / "config/ftbquests/quests/1.json").read_text("utf-8"))["title"] == "欢迎"
    assert (extract_root / "data/demo/advancement/t.json").exists()
    assert (extract_root / "kubejs/server_scripts/main.js").exists()
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
    patch = out_dir / "汉化补丁包.zip"
    assert patch.exists(), "VP 下载成功应产补丁包"
    with zipfile.ZipFile(patch) as zf:
        names = zf.namelist()
        assert "vault-patcher.jar" in names
        assert "vaultpatcher/modules/mc-auto-translator.json" in names
        assert zf.read("vault-patcher.jar") == b"vpjarbytes"
        module = json.loads(zf.read("vaultpatcher/modules/mc-auto-translator.json").decode("utf-8"))
    assert module[1]["pairs"]["Hello World"] == "你好世界"
    hard_dir = out_dir / "hardcoded"
    assert not hard_dir.exists() or not list(hard_dir.glob("*.jar")), "VP 方案启用不产 hardcoded 汉化 jar"


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
    await run_auto_translation(state.id, req, None, store, work, outputs)
    assert store.load(state.id).status == "done"
    out_dir = outputs / state.id
    hards = list((out_dir / "hardcoded").glob("*.jar"))
    assert hards, "下载失败应回退 hardcoded 汉化 jar"
    assert "你好世界" in " ".join(scan_hardcoded_strings(hards[0]))
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
    await run_auto_translation(state.id, req, None, store, work, outputs)
    st = store.load(state.id)
    assert st.status == "done"
    # total = 语言文件 1 + 硬编码候选 2
    assert st.total == 1 + 2
    # done 按候选数推进：语言文件 1 条 + 硬编码 2 条都算已处理
    assert st.done == 1 + 2
    assert st.done <= st.total


@pytest.mark.asyncio
async def test_auto_llm_total_includes_all_candidates(tmp_path, monkeypatch):
    """LLM 引擎：进度总量含全部硬编码候选（非只含 AI 判定可见），done 按候选数推进。"""
    from app.translate.llm import LLMClient

    mods = tmp_path / "mods"
    mods.mkdir()
    _make_jar_with_hardcode(mods, name="h.jar")   # 硬编码候选 "Hello World"（无语言文件）
    engine = LLMClient("https://x", "k", "m")
    monkeypatch.setattr("app.auto_flow.create_engine", lambda cfg: engine)

    async def fake_judge(engine, candidates, target, on_batch_done=None):
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
    await run_auto_translation(state.id, req, None, store, work, outputs)
    st = store.load(state.id)
    assert st.status == "done"
    # total = 硬编码候选数（无语言文件/文本源）
    assert st.total == 1
    # done 按候选数推进（AI 判定可见的 1 条）
    assert st.done == 1
    assert st.done <= st.total
