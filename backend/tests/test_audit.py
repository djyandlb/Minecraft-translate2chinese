# 官方术语质量审计测试：audit_translation 术语规则 / 占位符一致性 / 键名语义
from app.audit import audit_translation


def test_audit_iron_bars_pass():
    """Iron Bars → 铁栏杆（官方术语）通过，无 error。"""
    errors, warnings = audit_translation(
        {"mymod": {"k": "铁栏杆"}}, "zh_cn", {"mymod": {"k": "Iron Bars"}})
    assert errors == []


def test_audit_iron_bars_violation():
    """Iron Bars → 铁条（非官方术语）报 error。"""
    errors, warnings = audit_translation(
        {"mymod": {"k": "铁条"}}, "zh_cn", {"mymod": {"k": "Iron Bars"}})
    assert any("铁栏杆" in e["message"] for e in errors)


def test_audit_crimson_warped():
    """Crimson → 绯红、Warped → 诡异；译错报 error。"""
    errors, _ = audit_translation(
        {"m": {"a": "绯红", "b": "诡异"}}, "zh_cn",
        {"m": {"a": "Crimson", "b": "Warped"}})
    assert errors == []
    errors, _ = audit_translation(
        {"m": {"a": "深红"}}, "zh_cn", {"m": {"a": "Crimson"}})
    assert any("绯红" in e["message"] for e in errors)


def test_audit_placeholder_lost():
    """译文丢失占位符 → error。"""
    errors, _ = audit_translation(
        {"m": {"k": "你有 5 个物品"}}, "zh_cn", {"m": {"k": "你有 %s 个物品"}})
    assert any("占位符" in e["message"] for e in errors)


def test_audit_placeholder_kept():
    """译文保留占位符 → 无占位符 error。"""
    errors, _ = audit_translation(
        {"m": {"k": "你有 %s 个物品"}}, "zh_cn", {"m": {"k": "你有 %s 个物品"}})
    assert not any("占位符" in e["message"] for e in errors)


def test_audit_open_close_key():
    """键名语义：open/close 键译文必须含打开/关闭。"""
    errors, _ = audit_translation({"m": {"door.open": "打开门"}}, "zh_cn")
    assert not any("打开" in e["message"] or "开启" in e["message"] for e in errors)
    errors, _ = audit_translation({"m": {"door.open": "门"}}, "zh_cn")
    assert any("打开" in e["message"] or "开启" in e["message"] for e in errors)


def test_audit_planks():
    """Planks 必须保留「木板」材料语义。"""
    errors, _ = audit_translation(
        {"m": {"k": "橡木木板"}}, "zh_cn", {"m": {"k": "Oak Planks"}})
    assert errors == []
    errors, _ = audit_translation(
        {"m": {"k": "橡树"}}, "zh_cn", {"m": {"k": "Oak Planks"}})
    assert any("木板" in e["message"] for e in errors)


def test_audit_warnings_separate():
    """warning（如 Pane Window 玻璃板）与 error 分开返回。"""
    _, warnings = audit_translation(
        {"m": {"k": "窗户"}}, "zh_cn", {"m": {"k": "Pane Window"}})
    assert any("玻璃板" in w["message"] for w in warnings)
