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
