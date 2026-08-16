# 占位符保护模块测试
from app.placeholder import protect, restore, validate


def test_protect_keeps_format_codes():
    masked, markers = protect("铁锭 §a已获得 %1$s 个 {item} <item:iron_ingot> {{x}}")
    # 占位符被替换成 %%MC_ 标记，普通词保留
    assert "铁锭" in masked and "已获得" in masked
    assert masked.count("%%MC_") == 5 and markers
    for s in ("§a", "%1$s", "{item}", "<item:iron_ingot>", "{{x}}"):
        assert s not in masked


def test_restore_roundtrip():
    text = "got %s of §biron"
    masked, markers = protect(text)
    assert restore(masked, markers) == text


def test_restore_tolerates_bad_index():
    assert restore("x %%MC_99%% y", ["a"]) == "x %%MC_99%% y"


def test_parchment_placeholder_protected():
    """v1.3.6（用户「$O/$0 乱码」）：Parchment 富文本 $(item)/$() 必须整体保护——
    AI 翻译中间文本时不得破坏 $() 结构（实测被改成 $O/$0）。"""
    text = "$(item)Deforester Charge$() is for felling trees"
    masked, markers = protect(text)
    assert "$(item)" not in masked and "$()" not in masked
    assert masked.startswith("%%MC_0%%")   # $(item) 被替换
    assert "Deforester Charge" in masked     # 中间文本保留给 AI 翻译
    assert restore(masked, markers) == text  # 还原无损


def test_parchment_placeholder_roundtrip_pairs():
    """$(item)…$() 成对占位符：protect 全收编、restore 全还原、validate 可校验。"""
    text = "$(item)强化可控爆破装药$()的作用效果和$(item)可控爆破装药$()完全一致"
    masked, markers = protect(text)
    assert masked.count("%%MC_") == 4 and len(markers) == 4
    assert restore(masked, markers) == text
    # validate：占位符一致性校验能识别 Parchment 标记（原文含标记、译文丢了 → 判失败）
    assert validate(text, "$(item)强化可控爆破装药$()的作用效果和$(item)可控爆破装药$()完全一致")
    assert not validate(text, "强化可控爆破装药的作用效果和可控爆破装药完全一致")


def test_markdown_link_protected_as_token():
    """v1.4.0（用户「AE2 指南 [Subnetworksl../ae2- 碎片」）：Markdown 链接 `[text](url)`
    整个保护为一个 token——AI 低配模型把 `](` 输出成 `l`、截 URL、吃 `)` 尾括号，
    结构必然破坏；整体 token 化后 AI 物理上动不了结构，restore 原样贴回。"""
    # 链接文字 + URL 全保护
    text = "[Subnetworks](../ae2-mechanics/subnetworks.md)"
    masked, markers = protect(text)
    assert "Subnetworks" not in masked and "ae2" not in masked
    assert masked == "%%MC_0%%" and restore(masked, markers) == text
    # 正文段落内嵌链接：链接整体 token，前后正文留下
    para = "See [Wireless Access](wireless_access.md) for details."
    m2, mk2 = protect(para)
    assert "for details" in m2 and "Wireless" not in m2
    assert restore(m2, mk2) == para
    # 图片链接 ![alt](url) 同样保护
    img = "![Image description](block.png)"
    m3, mk3 = protect(img)
    assert m3 == "%%MC_0%%" and restore(m3, mk3) == img


def test_markdown_link_damage_caught_by_validate():
    """v1.4.0：链接整体 token 化后，AI 破坏结构（token 丢失/被改）→ validate 判失败。
    覆盖用户实测破坏形态：]( 变 l、URL 截断、尾括号丢失。"""
    text = "The [P2P Tunnels](../ae2-mechanics/machines/p2p_tunnels.md) block."
    masked, markers = protect(text)
    # AI 完整保留 masked → restore 后结构无损、validate 通过
    assert validate(text, restore(masked, markers))
    # 破坏：把 %%MC_0%% 换成 l（复现 ]( 变 l）
    assert not validate(text, "The l block.")
    # 破坏：token 被截断成 l 前缀
    assert not validate(text, "The %%MC_0%l block.")
    # 原文无链接的普通文本不受影响
    assert validate("plain text here", "普通文本这里")
