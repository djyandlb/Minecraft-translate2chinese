# -*- coding: utf-8 -*-
"""CFPA 社区词库模块测试。"""
import io
import json
import zipfile
from pathlib import Path

from app.cfpa import build_index, load_cfpa, match_zip_name


def _make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("assets/mymod/lang/zh_cn.json", json.dumps({
            "key.hello": "你好", "key.world": "世界"}))
        zf.writestr("assets/other/lang/zh_cn.json", json.dumps({"x.y": "译"}))
        zf.writestr("assets/mymod/lang/en_us.json", json.dumps({"key.hello": "Hello"}))  # en_us 不索引
    return buf.getvalue()


def test_match_zip_name():
    assert match_zip_name("1.20.1") == "Minecraft-Mod-Language-Modpack-1-20.zip"
    assert match_zip_name("1.21.4") == "Minecraft-Mod-Language-Modpack-1-21.zip"
    assert match_zip_name("1.19") == "Minecraft-Mod-Language-Modpack-1-19.zip"
    assert match_zip_name("1.17") == "Minecraft-Mod-Language-Modpack-1-16.zip"   # 回退到 ≤ 的最大支持版
    assert match_zip_name("1.9") is None                                          # 过老无匹配


def test_build_index():
    idx = build_index(_make_zip())
    assert idx["count"] == 3
    assert idx["by_key"]["mymod\x00key.hello"] == "你好"
    assert idx["by_key"]["mymod\x00key.world"] == "世界"
    assert idx["by_key"]["other\x00x.y"] == "译"
    # en_us.json 不进入索引
    assert "mymod\x00key.hello" in idx["by_key"]


def test_load_cfpa_missing():
    g = load_cfpa(Path("/nonexistent/cfpa_glossary.json"))
    assert g["by_key"] == {} and g["count"] == 0 and g["mc_version"] == ""


def test_load_cfpa_roundtrip(tmp_path):
    p = tmp_path / "cfpa_glossary.json"
    from app.cfpa import save_cfpa
    save_cfpa({"by_key": {"m\x00k": "译"}, "count": 1, "mc_version": "x.zip", "size_mb": 0.1}, p)
    g = load_cfpa(p)
    assert g["by_key"]["m\x00k"] == "译" and g["mc_version"] == "x.zip"
