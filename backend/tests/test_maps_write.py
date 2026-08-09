"""M4-4 地图副本写回测试。"""
from pathlib import Path
from nbtlib import File, Compound, String
from app.maps.write import apply_translation, write_translations


def test_apply_translation_nested_path(tmp_path: Path):
    nbt = File({"Data": Compound({"Command": String("say Hello")})})
    apply_translation(nbt["Data"], "Command", "说：你好")
    assert str(nbt["Data"]["Command"]) == "说：你好"


def test_apply_translation_list_index(tmp_path: Path):
    from nbtlib import List as NbtList
    nbt = File({"Data": Compound({"pages": NbtList([String("old"), String("second")])})})
    apply_translation(nbt["Data"], "pages[0]", "新内容")
    assert str(nbt["Data"]["pages"][0]) == "新内容"


def test_write_translations_dat(tmp_path: Path):
    p = tmp_path / "x.dat"
    File({"Data": Compound({"Command": String("say Hello")})}).save(p, gzipped=True)
    write_translations(p, [{"nbt_path": "Data.Command", "text": "say Hello", "translated": "说：你好"}])
    loaded = File.load(p, gzipped=True)
    assert str(loaded["Data"]["Command"]) == "说：你好"
    assert (tmp_path / "x.dat.bak").exists()   # 备份已建


def test_write_translations_json_text(tmp_path: Path):
    import json
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"title": {"text": "old text"}}), encoding="utf-8")
    write_translations(p, [{"nbt_path": "title.text", "text": "old text", "translated": "新标题"}])
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["title"]["text"] == "新标题"
