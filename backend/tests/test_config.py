import json
from pathlib import Path

import pytest

from app.config import AppConfig, DEFAULT_CONFIG

def test_defaults(tmp_path: Path):
    cfg = AppConfig(tmp_path / "cfg.json")
    assert cfg.get("engine") == "llm"
    assert cfg.get("target_lang") == "zh_cn"

def test_save_and_reload(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = AppConfig(p)
    cfg.set("target_lang", "zh_tw")
    cfg.save()
    assert AppConfig(p).get("target_lang") == "zh_tw"

def test_corrupted_json_falls_back(tmp_path: Path):
    # 损坏 json：构造不崩溃，默认值保留，坏文件被备份为 .bak
    p = tmp_path / "cfg.json"
    p.write_text("{bad json", encoding="utf-8")
    cfg = AppConfig(p)
    assert cfg.get("engine") == "llm"
    assert cfg.get("target_lang") == "zh_cn"
    assert (tmp_path / "cfg.json.bak").exists()

def test_non_dict_top_level_falls_back(tmp_path: Path):
    # 顶层不是 dict（如数组）：回退默认
    p = tmp_path / "cfg.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    cfg = AppConfig(p)
    assert cfg.get("engine") == "llm"
    assert cfg.get("target_lang") == "zh_cn"

def test_chinese_value_roundtrip_keeps_utf8(tmp_path: Path):
    # 中文值往返：文件内容含原文汉字，不含 中 转义（ensure_ascii=False）
    p = tmp_path / "cfg.json"
    cfg = AppConfig(p)
    cfg.set("target_lang", "中文测试")
    cfg.save()
    raw = p.read_text(encoding="utf-8")
    assert "中文测试" in raw
    assert "\\u4e2d" not in raw

def test_nested_dict_isolation(tmp_path: Path):
    # 嵌套隔离：改实例嵌套 dict 不污染 DEFAULT_CONFIG，也不影响新实例
    cfg1 = AppConfig(tmp_path / "cfg1.json")
    cfg1.get("llm")["model"] = "HACKED"
    assert DEFAULT_CONFIG["llm"]["model"] == ""   # 默认 llm 模板为空串（由 provider 主导）
    cfg2 = AppConfig(tmp_path / "cfg2.json")
    assert cfg2.get("llm")["model"] == ""

def test_api_key_guard(tmp_path: Path):
    # api_key 守卫：set 抛 ValueError；save 前自动剥离，绝不落盘
    cfg = AppConfig(tmp_path / "cfg.json")
    with pytest.raises(ValueError):
        cfg.set("api_key", "secret")
    cfg.data["api_key"] = "secret"  # 模拟意外写入
    cfg.save()
    saved = json.loads((tmp_path / "cfg.json").read_text(encoding="utf-8"))
    assert "api_key" not in saved
