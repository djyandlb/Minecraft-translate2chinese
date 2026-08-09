# 任务 A1 后端自动识别测试：detect_input_type / detect_source_lang / needs_translation / infer_pack_format + /api/detect 端点
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.detect import detect_input_type, detect_source_lang, needs_translation, infer_pack_format
from app.main import app


@pytest.fixture
def client():
    """参照 tests/test_api.py 顶部的 TestClient 用法。"""
    return TestClient(app)


def _make_mod_jar(tmp_path, name="mod.jar", lang="en_us", text="Hello World"):
    """造一个含 assets/mymod/lang/{lang}.json 的 mod jar（纯 zip 容器，无需 javac）。"""
    jar = tmp_path / name
    lang_path = f"assets/mymod/lang/{lang}.json"
    entries = {lang_path: json.dumps({"key.hello": text})}
    with zipfile.ZipFile(jar, "w") as zf:
        for p, content in entries.items():
            zf.writestr(p, content)
    return jar


def test_detect_input_type_dir(tmp_path):
    # 仅含 mods/ → modpack
    (tmp_path / "mods").mkdir()
    assert detect_input_type(tmp_path) == "modpack"
    # 含可加载 level.dat → map
    w = tmp_path / "world"; w.mkdir()
    from nbtlib import File, Compound, String
    File({"Data": Compound({"Command": String("say hi")})}).save(w / "level.dat", gzipped=True)
    assert detect_input_type(w) == "map"
    # 空目录 → unknown
    empty = tmp_path / "empty"; empty.mkdir()
    assert detect_input_type(empty) == "unknown"


def test_detect_input_type_file(tmp_path):
    # .jar → modjar（纯后缀轻量判断，不解压）
    jar = _make_mod_jar(tmp_path)
    assert detect_input_type(jar) == "modjar"
    # .mcworld → map
    mcworld = tmp_path / "w.mcworld"
    mcworld.write_bytes(b"PK\x05\x06\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00")
    assert detect_input_type(mcworld) == "map"
    # 其他文件 → unknown
    txt = tmp_path / "a.txt"; txt.write_text("x")
    assert detect_input_type(txt) == "unknown"


def test_detect_source_lang(tmp_path):
    jar_en = _make_mod_jar(tmp_path, name="a.jar", lang="en_us")
    jar_zh = _make_mod_jar(tmp_path, name="b.jar", lang="zh_cn")
    # en_us 与 zh_cn 同频，排除 target 后剩 en_us → 返回 en_us
    assert detect_source_lang([jar_en, jar_zh], "zh_cn") == "en_us"
    # 全部已汉化（只剩 target）→ None
    assert detect_source_lang([jar_zh], "zh_cn") is None


def test_needs_translation():
    # 纯 ASCII 空格串：should_translate 应保留（不是技术串）
    assert needs_translation("Hello World", "zh_cn") is True
    # 目标 zh 且含 CJK → 已汉化跳过
    assert needs_translation("你好世界", "zh_cn") is False
    # 技术标识符（snake_case）→ 不翻译
    assert needs_translation("iron_ingot", "zh_cn") is False
    # target 非 zh 时不做 CJK 跳过，走 should_translate
    assert needs_translation("你好", "en_us") is True


def test_infer_pack_format(tmp_path):
    # 根 pack.mcmeta 优先 → 22
    pm = tmp_path / "pack.mcmeta"
    pm.write_text(json.dumps({"pack": {"pack_format": 22}}), encoding="utf-8")
    assert infer_pack_format(tmp_path) == 22
    # 无 pack.mcmeta → 语言文件后缀推断：json → 15
    jar = _make_mod_jar(tmp_path, name="c.jar", lang="en_us")
    assert infer_pack_format(jar) in (15, 3)  # json → 15


def test_api_detect_modpack(tmp_path, client):
    # modpack 目录：识别 kind + 源语言 + summary
    mods = tmp_path / "mods"; mods.mkdir()
    _make_mod_jar(mods, name="m.jar")
    r = client.post("/api/detect", json={"path": str(tmp_path), "target_lang": "zh_cn"})
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "modpack"
    assert body["source_lang"] == "en_us"
    assert body["pack_format"] == 15
    assert body["summary"]["jar_count"] == 1


def test_api_detect_jar(tmp_path, client):
    # 单个 mod jar：kind=modjar
    jar = _make_mod_jar(tmp_path, name="mod.jar", lang="en_us")
    r = client.post("/api/detect", json={"path": str(jar), "target_lang": "zh_cn"})
    assert r.status_code == 200
    assert r.json()["kind"] == "modjar"
    assert r.json()["source_lang"] == "en_us"


def test_api_detect_unknown(tmp_path, client):
    # 无法识别 → kind=unknown 兜底，不带多余字段
    empty = tmp_path / "empty"; empty.mkdir()
    r = client.post("/api/detect", json={"path": str(empty), "target_lang": "zh_cn"})
    assert r.status_code == 200
    assert r.json()["kind"] == "unknown"
    assert "source_lang" not in r.json()


def test_summary_hardcoded_javac(tmp_path):
    # javac 真实编译带硬编码字符串的 class → summary 硬编码数真实统计（无 javac 跳过）
    import shutil
    import subprocess
    if not shutil.which("javac"):
        pytest.skip("无 javac")
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
    from app.detect import build_detect_summary
    s = build_detect_summary([jar], None)
    assert s["jar_count"] == 1
    assert s["jars"][0]["hardcoded"] is not None and s["jars"][0]["hardcoded"] >= 1


def test_api_detect_modpack_candidates(tmp_path, client):
    """modpack 目录（含硬编码 class jar）→ detect 返回 summary.hardcoded_candidates 非空候选列表。"""
    import shutil
    import subprocess
    if not shutil.which("javac"):
        pytest.skip("无 javac")
    mods = tmp_path / "mods"
    mods.mkdir()
    srcdir = tmp_path / "s"
    srcdir.mkdir()
    (srcdir / "HelloMod.java").write_text(
        'public class HelloMod { public static void main(String[] a) { System.out.println("Hello World"); } }',
        encoding="utf-8")
    classes = tmp_path / "c"
    classes.mkdir()
    subprocess.run(["javac", "-d", str(classes), str(srcdir / "HelloMod.java")], check=True)
    with zipfile.ZipFile(mods / "mod.jar", "w") as zf:
        for f in classes.rglob("*.class"):
            zf.write(f, f.relative_to(classes).as_posix())
    r = client.post("/api/detect", json={"path": str(tmp_path), "target_lang": "zh_cn"})
    assert r.status_code == 200
    body = r.json()
    cands = body["summary"]["hardcoded_candidates"]
    assert cands, "hardcoded_candidates 不应为空"
    assert any(c["text"] == "Hello World" and c["occurrences"] >= 1 for c in cands)
    assert body["summary"]["total_hardcoded"] == len(cands)


def test_api_detect_zip_archive(tmp_path, client, monkeypatch):
    # 整合包打成 .zip → 端点先 _resolve 解压再识别为 modpack（隔离 work 目录防污染）
    import shutil
    import app.main as main
    monkeypatch.setattr(main, "WORK_DIR", tmp_path / "work")
    src = tmp_path / "pkg"; src.mkdir()
    mods = src / "mods"; mods.mkdir()
    _make_mod_jar(mods, name="m.jar")
    archive = shutil.make_archive(str(tmp_path / "pkg"), "zip", src)
    r = client.post("/api/detect", json={"path": str(archive), "target_lang": "zh_cn"})
    assert r.status_code == 200
    assert r.json()["kind"] == "modpack"
    assert r.json()["source_lang"] == "en_us"
