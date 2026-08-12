import json
from pathlib import Path
from app.glossary import load_glossary, term_inject_prompt

def test_load(tmp_path: Path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"iron": "铁", "diamond": "钻石"}), encoding="utf-8")
    assert load_glossary(p) == {"iron": "铁", "diamond": "钻石"}

def test_missing_file_returns_empty(tmp_path: Path):
    assert load_glossary(tmp_path / "nope.json") == {}

def test_inject_prompt_limited():
    g = {f"k{i}": f"v{i}" for i in range(50)}
    prompt = term_inject_prompt(g, limit=10)
    assert prompt.count("=>") == 10
    assert "术语表" in prompt

def test_inject_prompt_empty():
    assert term_inject_prompt({}) == ""
