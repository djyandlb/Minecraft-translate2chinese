# 占位符保护模块测试
from app.placeholder import protect, restore


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
