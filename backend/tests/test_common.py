from app.translate.common import should_translate


def test_should_translate_filters_technical():
    assert not should_translate("iron_ingot")            # snake_case 标识符
    assert not should_translate("mods/demo/foo.class")   # 路径
    assert not should_translate("123")                   # 纯数字
    assert not should_translate("abc:def")               # 命名空间
    assert not should_translate("550e8400-e29b-41d4-a716-446655440000")  # UUID
    assert should_translate("How to craft an iron ingot")
    assert should_translate("铁锭")                      # 已含中文的文本保留可翻（含字母判断）


def test_should_translate_short_text():
    assert not should_translate("a")   # 太短
    assert should_translate("hi")
