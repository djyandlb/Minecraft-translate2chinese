from app.diff import compute_gaps, build_jobs
from app.scanner import ModScan


def test_gaps_skip_translated_include_empty():
    """已有非空翻译的 key 跳过；缺失或值为空的 key 都算缺口。"""
    src = {"a": "One", "b": "Two", "c": "Three"}
    existing = {"a": "一", "b": ""}
    assert compute_gaps(src, existing) == ["b", "c"]


def test_gaps_protect_url_and_identifier():
    """程序内部标识保护：URL / 路径 / 注册 ID（modid:key）形态不翻译。

    翻译这些会破坏资源位置 / Identifier（用户实测 Xaero 用翻译名拼 Identifier 崩溃）。
    """
    src = {"a": "https://example.com/xyz", "b": "/data/foo/bar",
           "c": "minecraft:iron_ingot", "d": "Hello World"}
    assert compute_gaps(src, {}, "zh_cn") == ["d"]


def test_gaps_force_effect_key_even_translated():
    """effect.* 强制纳入缺口：即使 zh_cn 已有中文值（旧汉化残留），也要重写英文防 Identifier 崩。"""
    src = {"effect.xaerominimap.no_minimap": "No Minimap", "gui.title": "Hello"}
    existing = {"effect.xaerominimap.no_minimap": "无小地图", "gui.title": "你好"}
    gaps = compute_gaps(src, existing, "zh_cn")
    assert "effect.xaerominimap.no_minimap" in gaps    # effect 强制重写
    assert "gui.title" not in gaps                      # 普通已汉化 key 跳过


def test_gaps_include_existing_english_value():
    """已有 zh_cn 值却是英文（未翻译占位）→ 补翻（Sodium 原版自带英文 zh_cn 场景）。"""
    src = {"a": "Low", "b": "Medium", "c": "High"}
    existing = {"a": "Low", "b": "中", "c": ""}        # a 英文值、b 已汉化、c 空值
    assert compute_gaps(src, existing, "zh_cn") == ["a", "c"]
    # 非 CJK 目标：值 == 源 → 未翻补翻；值 != 源 → 已翻跳过
    assert compute_gaps({"a": "Low", "b": "Medium"},
                        {"a": "Low", "b": "Moyen"}, "fr_fr") == ["a"]


def test_build_jobs_aggregates_mods():
    """多 mod 缺口汇总成翻译作业列表，跳过已有翻译的条目。"""
    scans = [
        ModScan(jar_path=None, modid="m1", source_entries={"x": "Hi"}, target_entries={}),
        ModScan(jar_path=None, modid="m2", source_entries={"y": "Bye"}, target_entries={"y": "再见"}),
    ]
    jobs = build_jobs(scans)
    assert [(j.modid, j.key, j.source_text) for j in jobs] == [("m1", "x", "Hi")]
