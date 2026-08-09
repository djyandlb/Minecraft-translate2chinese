import pytest
from app.translate.machine import MachineClient, map_lang


def test_map_lang():
    assert map_lang("zh_cn") == "zh-CN"
    assert map_lang("zh_tw") == "zh-TW"
    assert map_lang("en_us") == "en"
    assert map_lang("ja_jp") == "ja"
    assert map_lang("unknown") == "unknown"   # 未知原样返回


@pytest.mark.asyncio
async def test_translate_batch_uses_executor(monkeypatch):
    calls = []

    def fake_translate(src, tgt, text):
        calls.append((src, tgt, text))
        return "译文"

    import deep_translator
    monkeypatch.setattr(deep_translator, "GoogleTranslator",
                        lambda source, target: type("GT", (), {"translate": lambda self, t: fake_translate(source, target, t)})())
    client = MachineClient()
    out = await client.translate_batch(["hello"], "zh_cn")
    assert out == ["译文"] and len(calls) == 1


@pytest.mark.asyncio
async def test_technical_string_skips_translator(monkeypatch):
    import deep_translator
    monkeypatch.setattr(deep_translator, "GoogleTranslator",
                        lambda source, target: type("GT", (), {"translate": lambda self, t: "译"})())
    client = MachineClient()
    out = await client.translate_batch(["iron_ingot", "hi there"], "zh_cn")
    assert out == ["iron_ingot", "译"]   # 技术串跳过，不调 translator


@pytest.mark.asyncio
async def test_filter_technical_off_translates_snake_case(monkeypatch):
    """语言文件阶段关闭技术串过滤（filter_technical=False）：
    Requires_Armor 这类 snake_case 真实短语也调 translator 翻译。"""
    calls = []

    def fake_translate(src, tgt, text):
        calls.append((src, tgt, text))
        return "需要盔甲"

    import deep_translator
    monkeypatch.setattr(deep_translator, "GoogleTranslator",
                        lambda source, target: type("GT", (), {"translate": lambda self, t: fake_translate(source, target, t)})())
    client = MachineClient()
    client.filter_technical = False
    out = await client.translate_batch(["Requires_Armor"], "zh_cn")
    assert out == ["需要盔甲"] and len(calls) == 1
