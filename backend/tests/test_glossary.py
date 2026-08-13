import json
from pathlib import Path
from app.glossary import load_glossary, strip_particle, term_inject_prompt

def test_strip_particle():
    """词级术语译名剥离结尾格助词（防「符文的的宝珠」）：只剥结尾、多词短语不动、
    剥空/单字保护、连续助词全剥。"""
    assert strip_particle("符文的") == "符文"
    assert strip_particle("宝珠的") == "宝珠"
    assert strip_particle("狂乱的") == "狂乱"
    assert strip_particle("宝珠的符文") == "宝珠的符文"      # 多词短语结尾是名词 → 不动
    assert strip_particle("虚空之刃") == "虚空之刃"
    assert strip_particle("泽诺") == "泽诺"                  # 无助词不变
    assert strip_particle("刃的") == "刃的"                  # 剥成单字 → 保护原样
    assert strip_particle("的") == "的"                      # 纯助词 → 保护
    assert strip_particle("符文的的") == "符文"              # 连续助词全剥
    assert strip_particle("Zeno") == "Zeno"                  # 英文不变


def test_inject_prompt_strips_particle():
    """term_inject_prompt 注入前统一剥离结尾助词：记忆/用户表里的「符文的」不污染 AI。"""
    g = {"Rune": "符文的", "Zeno": "泽诺", "Blade": "刃的"}
    prompt = term_inject_prompt(g)
    assert "Rune => 符文" in prompt
    assert "Zeno => 泽诺" in prompt
    assert "Blade => 刃的" in prompt       # 单字剥离保护
    assert "Rune => 符文的" not in prompt


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
    assert "专有名词" in prompt     # v1.1.0：术语注入改为「专名对照（仅提示）」

def test_inject_prompt_empty():
    assert term_inject_prompt({}) == ""
