# 占位符增强测试：printf 完整格式说明符 + Minecraft 命令/路径 token + validate 一致性
from app.placeholder import extract_tokens, protect, restore, validate


def test_protect_printf_full_specifiers():
    """printf 完整格式说明符 %5.2f / %-10s / %+,.2f 被 protect 保留。"""
    text = "进度 %5.2f%% 收益 %-10s 价格 %+,.2f"
    masked, markers = protect(text)
    for tok in ("%5.2f", "%-10s", "%+,.2f"):
        assert tok not in masked, f"{tok} 未被保护"
    # %% 也被单独保护（printf 转义），markers 含它
    assert "%%" in markers
    assert restore(masked, markers) == text


def test_protect_printf_positional():
    """带位置参数 %1$s 与修饰符组合仍被保护。"""
    text = "收到 %1$5.2f 个礼物"
    masked, markers = protect(text)
    assert "%1$5.2f" not in masked
    assert restore(masked, markers) == text


def test_protect_minecraft_command():
    """Minecraft 命令 /give @p diamond 整体作为 token 保护，防止 AI 翻坏。"""
    text = "执行 /give @p diamond 指令"
    masked, markers = protect(text)
    assert "/give" not in masked
    assert "@p" not in masked
    assert restore(masked, markers) == text


def test_protect_path_token():
    """config/jei 类路径片段作为 token 保护。"""
    text = "请修改 config/jei/jei.toml 文件"
    masked, markers = protect(text)
    assert "/jei" not in masked
    assert restore(masked, markers) == text


def test_validate_placeholder_identical():
    """占位符一致：数量与内容逐一相等返回 True。"""
    assert validate("你有 %s 个物品", "你有 %s 个物品") is True
    assert validate("进度 %5.2f 元", "进度 %5.2f 元") is True


def test_validate_placeholder_lost():
    """占位符丢失：译文缺少 %s 返回 False。"""
    assert validate("你有 %s 个物品", "你有 5 个物品") is False


def test_validate_specifier_mismatch():
    """占位符内容不一致：%5.2f 被改成 %d 返回 False。"""
    assert validate("价格 %5.2f 元", "价格 %d 元") is False


def test_validate_minecraft_command_lost():
    """命令 token 丢失返回 False。"""
    assert validate("执行 /give @p diamond", "执行给予玩家钻石") is False


def test_validate_count_mismatch():
    """数量不一致：源两个占位符译文只有一个返回 False。"""
    assert validate("%s 和 %s", "%s") is False


def test_extract_tokens_order_preserved():
    """extract_tokens 返回按出现顺序的 token 列表（含重复）。"""
    tokens = extract_tokens("进度 %5.2f%% %s 次")
    assert tokens == ["%5.2f", "%%", "%s"]
