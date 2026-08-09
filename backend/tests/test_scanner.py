import json
import zipfile
from pathlib import Path
from app.scanner import scan_modpack, scan_jar


def _jar(path: Path, modid: str, en: dict, zh: dict):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"assets/{modid}/lang/en_us.json", json.dumps(en))
        zf.writestr(f"assets/{modid}/lang/zh_cn.json", json.dumps(zh))


def test_scan_jar_gaps(tmp_path: Path):
    jar = tmp_path / "demo.jar"
    _jar(jar, "demo", {"a": "One", "b": "Two"}, {"a": "一"})
    scans = scan_jar(jar, "en_us", "zh_cn")
    assert len(scans) == 1
    assert "b" in scans[0].source_entries and "b" not in scans[0].target_entries


def test_scan_modpack_collects_jars(tmp_path: Path):
    mods = tmp_path / "mods"
    mods.mkdir()
    _jar(mods / "m1.jar", "m1", {"k": "v"}, {})
    scans = scan_modpack(tmp_path, "en_us", "zh_cn")
    assert [s.modid for s in scans] == ["m1"]


def test_scan_modpack_scope_excludes_stray(tmp_path: Path):
    mods = tmp_path / "mods"; mods.mkdir()
    _jar(mods / "m1.jar", "m1", {"k": "v"}, {})
    _jar(tmp_path / "stray.jar", "stray", {"k": "v"}, {})   # mods 外，scope 默认 "mods" 应忽略
    scans = scan_modpack(tmp_path, "en_us", "zh_cn")
    assert [s.modid for s in scans] == ["m1"]


def test_scan_jar_lang_format(tmp_path: Path):
    jar = tmp_path / "demo2.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("assets/demo2/lang/en_us.lang", "a=One\nb=Two\n")
    scans = scan_jar(jar, "en_us", "zh_cn")
    assert scans[0].lang_format == "lang"
    assert scans[0].source_entries == {"a": "One", "b": "Two"}


def test_scan_jar_json_with_comments(tmp_path: Path):
    jar = tmp_path / "demo3.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("assets/demo3/lang/en_us.json",
                    '{\n// 行注释\n"a": "One",\n/* 块注释 */\n"b": "Two"\n}')
    scans = scan_jar(jar, "en_us", "zh_cn")
    assert len(scans) == 1
    assert scans[0].source_entries == {"a": "One", "b": "Two"}
