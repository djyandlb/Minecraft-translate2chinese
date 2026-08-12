# -*- coding: utf-8 -*-
"""汉化产物审查门禁测试。"""
import io
import json
import zipfile
from pathlib import Path

from app.verify import verify_translated_jar


def _jar_bytes(names_and_content: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n, c in names_and_content.items():
            zf.writestr(n, c)
    return buf.getvalue()


def test_verify_ok(tmp_path):
    jar = tmp_path / "ok.jar"
    jar.write_bytes(_jar_bytes({
        "fabric.mod.json": json.dumps({"id": "m", "version": "1"}),
        "assets/mymod/lang/zh_cn.json": json.dumps({"key.a": "你好"}),
    }))
    r = verify_translated_jar(jar)
    assert r["ok"] is True and r["issues"] == []


def test_verify_xaero_effect_non_ascii(tmp_path):
    """已知风险 mod（Xaero）的 effect 值非 ASCII → 软风险提示，不删产物（保留可下载）。

    语言文件阶段已保护 Xaero effect 保持英文；触发软风险的通常是用户已有的旧汉化 jar。
    """
    jar = tmp_path / "xaero.jar"
    jar.write_bytes(_jar_bytes({
        "fabric.mod.json": json.dumps({"id": "xaerominimap"}),
        "assets/xaerominimap/lang/zh_cn.json":
            json.dumps({"effect.xaerominimap.no_minimap": "无小地图"}),
    }))
    r = verify_translated_jar(jar)
    assert r["ok"] is True                 # 软风险不 fail，产物保留
    assert any("Identifier" in w for w in r["warnings"])


def test_verify_xaero_effect_ascii_ok(tmp_path):
    """Xaero effect 值保持英文（翻译阶段保护生效）→ 审查通过。"""
    jar = tmp_path / "xaero_ok.jar"
    jar.write_bytes(_jar_bytes({
        "fabric.mod.json": json.dumps({"id": "xaerominimap"}),
        "assets/xaerominimap/lang/zh_cn.json":
            json.dumps({"effect.xaerominimap.no_minimap": "No Minimap"}),
    }))
    assert verify_translated_jar(jar)["ok"] is True


def test_verify_no_metadata_ok(tmp_path):
    """无 mod 元数据不拦截（重打包保留全部条目；部分 jar 合法无标准元数据）。"""
    jar = tmp_path / "no_meta.jar"
    jar.write_bytes(_jar_bytes({"assets/mymod/lang/zh_cn.json": json.dumps({"k": "译"})}))
    assert verify_translated_jar(jar)["ok"] is True


def test_verify_other_lang_diacritics_ignored(tmp_path):
    """原版自带的其他语言（pl_pl 波兰语带 diacritics）不检查——只查目标语言 zh_cn。

    用户实测：Xaero 的 pl_pl effect 值含 diacritics 被 isascii() 误判拦截 → 产物被删。
    """
    jar = tmp_path / "xaero_pl.jar"
    jar.write_bytes(_jar_bytes({
        "fabric.mod.json": json.dumps({"id": "xaerominimap"}),
        "assets/xaerominimap/lang/zh_cn.json":
            json.dumps({"effect.xaerominimap.no_minimap": "No Minimap"}),   # 目标语言 effect 英文 ✓
        "assets/xaerominimap/lang/pl_pl.json":
            json.dumps({"effect.xaerominimap.no_minimap": "Nie minimapy"}),  # 原版 pl_pl 不检查
    }))
    assert verify_translated_jar(jar)["ok"] is True
    # 目标语言 zh_cn 的 effect 非 ASCII → 软风险提示（不删产物）
    jar2 = tmp_path / "xaero_bad.jar"
    jar2.write_bytes(_jar_bytes({
        "fabric.mod.json": json.dumps({"id": "xaerominimap"}),
        "assets/xaerominimap/lang/zh_cn.json":
            json.dumps({"effect.xaerominimap.no_minimap": "无小地图"}),
    }))
    r2 = verify_translated_jar(jar2)
    assert r2["ok"] is True
    assert any("Identifier" in w for w in r2["warnings"])


def test_verify_broken_lang(tmp_path):
    """语言文件损坏（非 JSON）→ 审查不过。"""
    jar = tmp_path / "bad.jar"
    jar.write_bytes(_jar_bytes({
        "fabric.mod.json": json.dumps({"id": "m"}),
        "assets/mymod/lang/zh_cn.json": b"{broken",
    }))
    r = verify_translated_jar(jar)
    assert r["ok"] is False
