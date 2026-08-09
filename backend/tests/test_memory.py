from app.memory import MemoryStore


def test_memory_roundtrip(tmp_path):
    # 翻译记忆往返：写入保存后重新加载应能命中
    m = MemoryStore(tmp_path / "mem.json")
    assert m.get("hi", "zh_cn") is None
    m.set("hi", "zh_cn", "嗨")
    m.save()
    m2 = MemoryStore(tmp_path / "mem.json")
    assert m2.get("hi", "zh_cn") == "嗨"


def test_memory_unicode(tmp_path):
    # ensure_ascii=False：中文译文应原文落盘，而非 \u 转义
    m = MemoryStore(tmp_path / "mem.json")
    m.set("Iron Ingot", "zh_cn", "铁锭")
    m.save()
    raw = (tmp_path / "mem.json").read_text(encoding="utf-8")
    assert "铁锭" in raw
    assert "\\u94c1" not in raw


def test_memory_lang_isolation(tmp_path):
    # F3：同一原文不同目标语言互不覆盖，zh_cn 记忆不命中 zh_tw，set 不互相污染
    m = MemoryStore(tmp_path / "mem.json")
    assert m.get("hi", "zh_tw") is None
    m.set("hi", "zh_cn", "嗨")
    m.set("hi", "zh_tw", "哈囉")
    assert m.get("hi", "zh_cn") == "嗨"
    assert m.get("hi", "zh_tw") == "哈囉"
    m.save()
    m2 = MemoryStore(tmp_path / "mem.json")
    assert m2.get("hi", "zh_cn") == "嗨"
    assert m2.get("hi", "zh_tw") == "哈囉"
