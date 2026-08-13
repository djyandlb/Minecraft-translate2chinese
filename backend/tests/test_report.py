# -*- coding: utf-8 -*-
"""翻译报告（report.py）测试：覆盖率计算（v1.2.0：扣减跳过翻译的可翻译量）。"""
from app.report import build_report


def test_build_report_coverage_skips_skipped():
    """覆盖率 = 成功 / 可翻译量（总文本 - 不算 failed 但跳过翻译的量）。
    用户诉求：total=10000 含 2000 硬编码 AI 判定不用翻译 → 可翻译量 8000；
    done 含跳过（skip/排除都推进 done）→ 成功 = done - skipped。"""
    # 全成功：8000 成功 + 2000 跳过（done 含跳过=10000）→ 覆盖率 100%
    r = build_report("x", "zh_cn", 10000, 10000, 0, [], [], [], skipped=2000)
    assert r["overview"]["total"] == 10000
    assert r["overview"]["translated"] == 8000          # done(10000) - skipped(2000)
    assert r["overview"]["coverage"] == 100.0           # 8000 / (10000-2000)
    # 有失败：7000 成功 + 2000 跳过 + 1000 失败 → done=9000(含跳过)，成功=7000
    r2 = build_report("x", "zh_cn", 10000, 9000, 1000, [], [], [], skipped=2000)
    assert r2["overview"]["translated"] == 7000
    assert r2["overview"]["coverage"] == round(7000 / 8000 * 100, 2)   # 87.5
    # 无跳过：保持旧行为（成功/总量）
    r3 = build_report("x", "zh_cn", 10000, 9000, 1000, [], [], [])
    assert r3["overview"]["translated"] == 9000
    assert r3["overview"]["coverage"] == 90.0
    # 跳过超过 total（异常保护）：分母至少 1
    r4 = build_report("x", "zh_cn", 100, 100, 0, [], [], [], skipped=200)
    assert r4["overview"]["coverage"] == 0.0           # 可翻译量 0
