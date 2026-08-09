import json
import zipfile
from pathlib import Path
from app.jar import list_jar_lang_files, extract_jar_to, pack_dir_to_jar


def _make_jar(path: Path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/mods.toml", 'modId="demo"\n')
        zf.writestr("assets/demo/lang/en_us.json", json.dumps({"item.x": "Iron"}))
        zf.writestr("assets/demo/lang/zh_cn.json", json.dumps({"item.x": "铁"}))


def test_list_lang_files(tmp_path: Path):
    jar = tmp_path / "demo.jar"
    _make_jar(jar)
    files = list_jar_lang_files(jar)
    langs = {f["lang"] for f in files}
    assert langs == {"en_us", "zh_cn"}
    assert all(f["modid"] == "demo" for f in files)


def test_pack_roundtrip(tmp_path: Path):
    jar = tmp_path / "a.jar"
    _make_jar(jar)
    out = tmp_path / "out"
    extract_jar_to(jar, out)
    repacked = tmp_path / "b.jar"
    pack_dir_to_jar(out, repacked)
    # dict 不可哈希，不能用 set 比较；按 path 排序后整体比较，语义等价
    key = lambda d: d["path"]
    assert sorted(list_jar_lang_files(repacked), key=key) == sorted(
        list_jar_lang_files(jar), key=key
    )
