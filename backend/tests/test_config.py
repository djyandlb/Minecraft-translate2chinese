from pathlib import Path
from app.config import AppConfig

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
