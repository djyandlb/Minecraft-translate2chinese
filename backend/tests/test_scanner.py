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


def test_scan_jar_lang_snake_case_value(tmp_path: Path):
    # 语言文件值 snake_case 形态（Requires_Armor）是真实可翻译短语，不被技术串规则滤掉
    jar = tmp_path / "snake.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("assets/snake/lang/en_us.json",
                    json.dumps({"item.armor": "Requires_Armor"}))
    scans = scan_jar(jar, "en_us", "zh_cn")
    assert len(scans) == 1
    assert scans[0].source_entries.get("item.armor") == "Requires_Armor"


def test_scan_jar_lang_pure_digit_value_filtered(tmp_path: Path):
    # 纯数字/过短的语言文件值不值得翻译：宽松过滤仅长度 2-200 + 含字母
    jar = tmp_path / "digit.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("assets/digit/lang/en_us.json",
                    json.dumps({"num": "123", "short": "x", "txt": "Hello"}))
    scans = scan_jar(jar, "en_us", "zh_cn")
    assert len(scans) == 1
    assert scans[0].source_entries == {"txt": "Hello"}


def test_scan_modpack_skips_broken_jar(tmp_path: Path):
    # 坏 jar（纯文本非 zip）+ 好 jar → 只扫出好的，不抛异常
    mods = tmp_path / "mods"; mods.mkdir()
    (mods / "broken.jar").write_text("这不是一个 zip 文件", encoding="utf-8")
    _jar(mods / "good.jar", "good", {"k": "v"}, {})
    scans = scan_modpack(tmp_path, "en_us", "zh_cn")
    assert [s.modid for s in scans] == ["good"]


def test_scan_modpack_skips_corrupt_lang_json(tmp_path: Path):
    # jar 本身合法 zip，但语言文件 json 语法损坏 → 跳过该 jar
    mods = tmp_path / "mods"; mods.mkdir()
    bad = mods / "bad_json.jar"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("assets/bad_json/lang/en_us.json", "{ 这不是合法 json")
    _jar(mods / "good.jar", "good", {"k": "v"}, {})
    scans = scan_modpack(tmp_path, "en_us", "zh_cn")
    assert [s.modid for s in scans] == ["good"]
