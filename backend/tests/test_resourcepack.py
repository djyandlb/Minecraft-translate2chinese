import json
import zipfile
from pathlib import Path
from app.resourcepack import pack_mcmeta, build_resource_pack

def test_pack_meta():
    meta = pack_mcmeta(15)
    assert meta["pack"]["pack_format"] == 15

def test_build_resource_pack_json(tmp_path: Path):
    out = tmp_path / "zh_cn.zip"
    build_resource_pack({"demo": {"item.x": "铁"}}, "zh_cn", 15, out)
    with zipfile.ZipFile(out) as zf:
        assert "pack.mcmeta" in zf.namelist()
        lang = json.loads(zf.read("assets/demo/lang/zh_cn.json"))
        assert lang == {"item.x": "铁"}

def test_build_resource_pack_lang_ext(tmp_path: Path):
    # pack_format=3（1.12）→ 语言文件用 .lang
    out = tmp_path / "zh_cn.zip"
    build_resource_pack({"demo": {"item.x": "铁"}}, "zh_cn", 3, out)
    with zipfile.ZipFile(out) as zf:
        assert "assets/demo/lang/zh_cn.lang" in zf.namelist()
