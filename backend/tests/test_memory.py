from app.memory import MemoryStore


def test_memory_roundtrip(tmp_path):
    # 翻译记忆往返：写入保存后重新加载应能命中
    m = MemoryStore(tmp_path / "mem.json")
    assert m.get("hi") is None
    m.set("hi", "嗨")
    m.save()
    m2 = MemoryStore(tmp_path / "mem.json")
    assert m2.get("hi") == "嗨"


def test_memory_unicode(tmp_path):
    # ensure_ascii=False：中文译文应原文落盘，而非 \u 转义
    m = MemoryStore(tmp_path / "mem.json")
    m.set("Iron Ingot", "铁锭")
    m.save()
    raw = (tmp_path / "mem.json").read_text(encoding="utf-8")
    assert "铁锭" in raw
    assert "\\u94c1" not in raw
