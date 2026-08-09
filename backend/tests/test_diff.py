from app.diff import compute_gaps, build_jobs
from app.scanner import ModScan


def test_gaps_skip_translated_include_empty():
    """已有非空翻译的 key 跳过；缺失或值为空的 key 都算缺口。"""
    src = {"a": "One", "b": "Two", "c": "Three"}
    existing = {"a": "一", "b": ""}
    assert compute_gaps(src, existing) == ["b", "c"]


def test_build_jobs_aggregates_mods():
    """多 mod 缺口汇总成翻译作业列表，跳过已有翻译的条目。"""
    scans = [
        ModScan(jar_path=None, modid="m1", source_entries={"x": "Hi"}, target_entries={}),
        ModScan(jar_path=None, modid="m2", source_entries={"y": "Bye"}, target_entries={"y": "再见"}),
    ]
    jobs = build_jobs(scans)
    assert [(j.modid, j.key, j.source_text) for j in jobs] == [("m1", "x", "Hi")]
