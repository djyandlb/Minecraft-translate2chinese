# -*- coding: utf-8 -*-
"""AI 译文质量审查（review.py）测试：解析审查输出 + 审查 prompt 构造。"""
from app.review import _review_prompt, parse_review


def test_parse_review_marks_bad_entries():
    """不合格条目带 [iN] 前缀 + 原因被正确解析。"""
    out = parse_review("[i0] 译文截断\n[i2] 中英混杂", 4)
    assert out == {0: "译文截断", 2: "中英混杂"}


def test_parse_review_all_pass():
    """整批合格标记 → 空（不误伤）。"""
    assert parse_review("全部合格", 5) == {}
    assert parse_review("无不合格条目", 5) == {}


def test_parse_review_ignores_non_marked_lines():
    """无编号说明行 / 越界索引忽略。"""
    out = parse_review("审查完毕\n[i1] 生硬\n说明文字", 3)
    assert out == {1: "生硬"}


def test_parse_review_variant_prefix():
    """兼容 *iN* / **iN** 变体。"""
    out = parse_review("*i0* 机翻腔\n**i3** 偏离原文", 5)
    assert out == {0: "机翻腔", 3: "偏离原文"}


def test_review_prompt_contains_rules_and_pairs():
    """prompt 含审查标准 + 逐行 [iN] 源 ||| 译。"""
    p = _review_prompt([{"source": "Hello World", "translated": "你好世界"},
                        {"source": "Welcome", "translated": "欢迎"}], "zh_cn")
    assert "[i0] Hello World ||| 你好世界" in p
    assert "[i1] Welcome ||| 欢迎" in p
    assert "截断" in p and "中英混杂" in p        # 审查标准在
    assert "助词冗余" in p and "的的" in p        # 助词冗余检查（防「符文的的宝珠」/「基于的」）
