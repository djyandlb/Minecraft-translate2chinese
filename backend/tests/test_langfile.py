from pathlib import Path
from app.langfile import parse_lang, parse_json_lang, load_lang_file, write_json_lang, write_lang

def test_parse_lang_ignores_comments():
    text = "# 注释\nitem.iron=铁锭\nitem.gold = 金锭\n"
    assert parse_lang(text) == {"item.iron": "铁锭", "item.gold": "金锭"}

def test_parse_json_with_comments():
    text = '{\n  // 注释\n  "item.iron": "铁锭",\n  "item.gold": "金锭"\n}'
    assert parse_json_lang(text) == {"item.iron": "铁锭", "item.gold": "金锭"}

def test_load_and_write_json_roundtrip(tmp_path: Path):
    p = tmp_path / "a.json"
    p.write_text('{"k": "值"}', encoding="utf-8")
    entries, fmt = load_lang_file(p)
    assert fmt == "json" and entries == {"k": "值"}
    out = p.with_name("out.json")
    out.write_text(write_json_lang(entries), encoding="utf-8")
    assert load_lang_file(out)[0] == {"k": "值"}

def test_lang_roundtrip(tmp_path: Path):
    p = tmp_path / "b.lang"
    p.write_text("k=值\n", encoding="utf-8")
    entries, fmt = load_lang_file(p)
    assert fmt == "lang" and entries == {"k": "值"}
    out = p.with_name("out.lang")
    out.write_text(write_lang(entries), encoding="utf-8")
    assert load_lang_file(out)[0] == {"k": "值"}
