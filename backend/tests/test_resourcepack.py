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

def test_build_resource_pack_allows_valid_modid_chars(tmp_path: Path):
    # 连字符与下划线是标准 modid 字符，应正常放行
    out = tmp_path / "ok.zip"
    build_resource_pack({"my_mod-2": {"item.x": "铁"}}, "zh_cn", 15, out)
    with zipfile.ZipFile(out) as zf:
        assert "assets/my_mod-2/lang/zh_cn.json" in zf.namelist()

def test_build_resource_pack_rejects_path_traversal_modid(tmp_path: Path):
    # 恶意 modid：".."、"a/b"、"../evil" 一律不写入，zip 内只留 pack.mcmeta + pack.png 图标
    out = tmp_path / "evil.zip"
    evil = {"..": {"item.x": "铁"}, "a/b": {"item.x": "铁"}, "../evil": {"item.x": "铁"}}
    build_resource_pack(evil, "zh_cn", 15, out)
    with zipfile.ZipFile(out) as zf:
        assert set(zf.namelist()) == {"pack.mcmeta", "pack.png"}   # pack.png 为资源包图标（图标接入）

def test_build_resource_pack_rejects_bad_target_lang(tmp_path: Path):
    # target_lang 同为不可信外部输入：含 "/"、".." 时跳过全部语言文件写入
    out = tmp_path / "bad_lang.zip"
    build_resource_pack({"demo": {"item.x": "铁"}}, "../zh_cn", 15, out)
    with zipfile.ZipFile(out) as zf:
        assert set(zf.namelist()) == {"pack.mcmeta", "pack.png"}
