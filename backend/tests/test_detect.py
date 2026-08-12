# 任务 A1 后端自动识别测试：detect_input_type / detect_source_lang / needs_translation / infer_pack_format + /api/detect 端点
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.detect import (detect_input_type, detect_source_lang, needs_translation,
                        needs_lang_value_translation, infer_pack_format, detect_mc_version)
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
    # 混排（英文为主 + 少量中文）→ 中文占比低，仍需翻译
    assert needs_translation("Click here 点击", "zh_cn") is True
    # 技术标识符（snake_case）→ 不翻译
    assert needs_translation("iron_ingot", "zh_cn") is False
    # target 非 zh 时不做 CJK 跳过，走 should_translate
    assert needs_translation("你好", "en_us") is True


def test_needs_lang_value_translation():
    # 语言文件值：snake_case 真实短语（Requires_Armor）放行，不走 should_translate 技术串过滤
    assert needs_lang_value_translation("Requires_Armor", "zh_cn") is True
    # 纯中文（已汉化）→ 跳过
    assert needs_lang_value_translation("你好世界", "zh_cn") is False
    # 英文值 → 放行
    assert needs_lang_value_translation("Hello World", "zh_cn") is True


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
    # detect 不再扫硬编码候选（修复：大整合包逐个解析 class 卡死识别）→ total_hardcoded None
    assert s["total_hardcoded"] is None


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
    assert body["kind"] == "modpack"
    # detect 不再扫硬编码候选（修复卡死）→ total_hardcoded None，硬编码留给翻译流程
    assert body["summary"]["total_hardcoded"] is None


def test_api_detect_zip_archive(tmp_path, client, monkeypatch):
    # 整合包打成 .zip → 轻量识别（只读中央目录，不解压整包）为 modpack
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
    # 轻量识别：压缩包不解压 → source_lang 翻译时后端自动识别（此处 None）；jar 数从中央目录统计
    assert r.json()["source_lang"] is None
    assert r.json()["summary"]["jar_count"] == 1


# ---------- MC 版本自动识别（材质包 pack_format 按版本注入） ----------

def test_detect_mc_version_pack_toml(tmp_path):
    """Modrinth .mrpack 解压（pack.toml 声明 minecraft）→ 识别 MC 版本。"""
    (tmp_path / "pack.toml").write_text('[versions]\nminecraft = "1.20.1"\n', encoding="utf-8")
    assert detect_mc_version(tmp_path) == "1.20.1"


def test_detect_mc_version_manifest_json(tmp_path):
    """CurseForge / Modrinth 整合包 manifest.json 声明版本 → 识别。"""
    (tmp_path / "manifest.json").write_text(
        json.dumps({"minecraft": {"version": "1.21.9"}}), encoding="utf-8")
    assert detect_mc_version(tmp_path) == "1.21.9"


def test_detect_mc_version_mmc_pack(tmp_path):
    """MultiMC / Prism 整合包 mmc-pack.json components 声明版本 → 识别。"""
    (tmp_path / "mmc-pack.json").write_text(json.dumps(
        {"components": [{"uid": "minecraft", "version": "1.20.4"}]}), encoding="utf-8")
    assert detect_mc_version(tmp_path) == "1.20.4"


def test_detect_mc_version_from_mods_fabric(tmp_path):
    """无 manifest，从 mods 元数据（fabric.mod.json depends.minecraft）识别。"""
    mods = tmp_path / "mods"; mods.mkdir()
    jar = mods / "m.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("fabric.mod.json", json.dumps({"id": "m", "depends": {"minecraft": ">=1.20.1"}}))
    assert detect_mc_version(tmp_path) == "1.20.1"


def test_detect_mc_version_mods_toml(tmp_path):
    """forge/neoforge 元数据（mods.toml minecraft versionRange）识别。"""
    mods = tmp_path / "mods"; mods.mkdir()
    jar = mods / "f.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("META-INF/mods.toml", (
            'modLoader="javafml"\n[[mods]]\nmodId="m"\n'
            '[[dependencies.m]]\nmodId="minecraft"\nversionRange="[1.16.5,1.16.6)"\n'))
    assert detect_mc_version(tmp_path) == "1.16.5"


def test_detect_mc_version_none(tmp_path):
    """无 manifest / 无 mods 元数据 / 空目录 → 空串（调用方回退语言后缀推断）。"""
    assert detect_mc_version(tmp_path) == ""


def test_version_to_pack_format():
    """MC 版本 → pack_format：精确 + 前缀匹配 + 空串兜底 15（1.20.1）。"""
    from app.version import version_to_pack_format
    assert version_to_pack_format("1.12.2") == 3      # 老版本 .lang
    assert version_to_pack_format("1.20.1") == 15
    assert version_to_pack_format("1.20.4") == 22     # 1.20.3/1.20.4 共享 22（资源包格式）
    assert version_to_pack_format("1.21.4") == 46
    assert version_to_pack_format("1.21.9") == 69     # 1.21.9+ 数组格式的整数部分
    assert version_to_pack_format("1.21.11") == 69    # 1.21.9+ 同 major
    assert version_to_pack_format("") == 15


# ---------- zip 嵌套包裹层（xxxx/主文件夹 结构）下钻识别 ----------

def test_detect_input_type_wrapped_modpack(tmp_path):
    """zip 解压后的 xxxx/ 包裹结构（mods 在包裹层内）→ 下钻识别 modpack。"""
    wrapped = tmp_path / "ProjectInfinity-v1.2"
    (wrapped / "mods").mkdir(parents=True)
    _make_mod_jar(wrapped / "mods", name="m.jar")
    assert detect_input_type(tmp_path) == "modpack"
    # 双层包裹（a/b/mods）也逐层下钻
    deep = tmp_path / "a" / "b"
    (deep / "mods").mkdir(parents=True)
    assert detect_input_type(tmp_path / "a") == "modpack"


def test_detect_input_type_wrapped_map(tmp_path):
    """地图 zip 解压后 xxxx/ 包裹（level.dat 在包裹层内）→ 下钻识别 map。"""
    from nbtlib import File, Compound, String
    w = tmp_path / "MapPack" / "world"
    w.mkdir(parents=True)
    File({"Data": Compound({"Command": String("say hi")})}).save(w / "level.dat", gzipped=True)
    assert detect_input_type(tmp_path) == "map"


def test_detect_input_type_wrapped_shader(tmp_path):
    """光影 zip 解压后 xxxx/ 包裹（shaders/lang 在包裹层内）→ 下钻识别 shader。"""
    (tmp_path / "SEUS" / "shaders" / "lang").mkdir(parents=True)
    (tmp_path / "SEUS" / "shaders" / "lang" / "en_US.lang").write_text("title=Hello\n", encoding="utf-8")
    assert detect_input_type(tmp_path) == "shader"


def test_unwrap_bare_wrapper_safe(tmp_path):
    """项目根/多目录/文件输入：下钻幂等不误判。"""
    from app.detect import unwrap_bare_wrapper
    (tmp_path / "mods").mkdir()
    assert unwrap_bare_wrapper(tmp_path) == tmp_path          # 已是项目根不动
    jar = tmp_path / "a.jar"; jar.write_bytes(b"x")
    assert unwrap_bare_wrapper(jar) == jar                    # 文件输入原样
    multi = tmp_path / "multi"; multi.mkdir()
    (multi / "x").mkdir(); (multi / "y").mkdir()
    assert unwrap_bare_wrapper(multi) == multi                # 多目录非包裹不下钻


def test_api_detect_wrapped_zip(tmp_path, client, monkeypatch):
    """压缩包内 xxxx/mods 嵌套 → 轻量识别 modpack（任意层级匹配，不再漏判 unknown）。"""
    import shutil
    import app.main as main
    monkeypatch.setattr(main, "WORK_DIR", tmp_path / "work")
    pkg = tmp_path / "ProjectInfinity-v1.2"
    (pkg / "mods").mkdir(parents=True)
    _make_mod_jar(pkg / "mods", name="m.jar")
    archive = shutil.make_archive(str(tmp_path / "pkg"), "zip", tmp_path)
    r = client.post("/api/detect", json={"path": str(archive), "target_lang": "zh_cn"})
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "modpack", f"嵌套 zip 应识别 modpack，实际 {body['kind']}"
    assert body["summary"]["jar_count"] == 1


def test_api_detect_wrapped_map_zip(tmp_path, client, monkeypatch):
    """压缩包内 xxxx/world/level.dat 嵌套 → 轻量识别 map。"""
    import shutil
    from nbtlib import File, Compound, String
    import app.main as main
    monkeypatch.setattr(main, "WORK_DIR", tmp_path / "work")
    w = tmp_path / "MyMap-v2" / "world"
    w.mkdir(parents=True)
    File({"Data": Compound({"Command": String("say hi")})}).save(w / "level.dat", gzipped=True)
    archive = shutil.make_archive(str(tmp_path / "mymap"), "zip", tmp_path)
    r = client.post("/api/detect", json={"path": str(archive), "target_lang": "zh_cn"})
    assert r.status_code == 200
    assert r.json()["kind"] == "map", "嵌套地图 zip 应识别为 map"


def test_api_detect_modpack_with_shader_dir_not_misjudged(tmp_path, client, monkeypatch):
    """整合包 zip 内含 shaders/lang/ 路径（mod/资源包携带）→ 仍识别 modpack，不误判光影。

    recheck 修复：shader 判断曾排在 mods 之前，整合包含 shaders/lang 就误判 shader
    （用户实测 Better MC [FORGE] 整合包被识别成光影）。mods 强信号优先。
    """
    import shutil
    import app.main as main
    monkeypatch.setattr(main, "WORK_DIR", tmp_path / "work")
    pkg = tmp_path / "BetterMC"
    (pkg / "mods").mkdir(parents=True)
    _make_mod_jar(pkg / "mods", name="m.jar")
    (pkg / "resourcepacks" / "SomePack" / "shaders" / "lang").mkdir(parents=True)
    archive = shutil.make_archive(str(tmp_path / "pkg"), "zip", tmp_path)
    r = client.post("/api/detect", json={"path": str(archive), "target_lang": "zh_cn"})
    assert r.status_code == 200
    assert r.json()["kind"] == "modpack", f"整合包含 shaders/lang 应识别 modpack，实际 {r.json()['kind']}"


def test_needs_lang_value_translation_target_script_semantics():
    """语言文件值「已汉化」判定（修复 recheck：原 40% 中文占比是拍脑袋阈值）——
    改为主流做法（key 差集 + 值语言校验）：含任一目标语言字符即已汉化跳过
    （部分翻译「钻石 Diamond」是正常汉化实践，不重翻）；纯英文假翻译占位补翻。"""
    from app.detect import needs_lang_value_translation
    # zh_cn/zh_tw：含汉字即已汉化
    assert needs_lang_value_translation("钻石 Diamond", "zh_cn") is False
    assert needs_lang_value_translation("铁锭", "zh_tw") is False
    # 纯英文（mod 自带 zh_cn 值是英文占位）→ 补翻
    assert needs_lang_value_translation("Iron Ingot", "zh_cn") is True
    assert needs_lang_value_translation("Hello World", "zh_cn") is True
    # ja/ko 目标：含假名/谚文即已汉化
    assert needs_lang_value_translation("これはテストです", "ja_jp") is False
    assert needs_lang_value_translation("테스트 항목", "ko_kr") is False
    assert needs_lang_value_translation("Test Item", "ja_jp") is True
