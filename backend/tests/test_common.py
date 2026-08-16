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


def test_should_translate_markdown_tutorial():
    """v1.3.9 修复（用户「AE2 教程 markdown 没翻译 / line31/35 原文残留」）：
    markdown 标题（# Wireless Terminal）、链接 [text](url)、教程正文是用户可见文本，
    此前被 should_translate 当技术串跳过 → 翻译阶段 AI 没收到 → 原文直接写回。
    现在标题/链接/正文都放行；真技术串（shader 指令/命令/下标/纯路径）仍跳过。"""
    from app.translate.common import should_translate
    # 应翻译：markdown 教程文本
    for t in [
        "# Wireless Terminal",
        "## The Fluix Block",
        "### Sub Title",
        "[items-blocks-machines/wireless_terminals.md] 导读",   # 链接 + 正文（含空格）
        "Your basic terminal, now portable! View and access the contents of your "
        "[network's storage](.../ae2-mechanics/import-export-storage.md)",   # 链接文字+正文
        "[click here](https://example.com)",
    ]:
        assert should_translate(t), f"应翻译却跳过: {t}"
    # 应跳过：真技术串
    for t in [
        "#version 120",
        "#define MAX 10",
        "/give @p diamond",
        "[i0] text",
        "@param x",
        "config/jei/jei.toml",
        "[items-blocks-machines/wireless_terminals.md]",   # 纯路径链接（无文字）→ 保留
    ]:
        assert not should_translate(t), f"应跳过却翻译: {t}"
