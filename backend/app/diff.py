from dataclasses import dataclass

from app.scanner import ModScan


def compute_gaps(source: dict[str, str], existing: dict[str, str]) -> list[str]:
    """返回 source 中缺失或值为空的 key（已有翻译的不翻）。"""
    return [k for k in source if k not in existing or not existing[k].strip()]


@dataclass
class TranslationJob:
    modid: str
    key: str
    source_text: str


def build_jobs(scans: list[ModScan]) -> list[TranslationJob]:
    """把所有 mod 的翻译缺口汇总成作业列表。"""
    jobs: list[TranslationJob] = []
    for scan in scans:
        for key in compute_gaps(scan.source_entries, scan.target_entries):
            jobs.append(TranslationJob(scan.modid, key, scan.source_entries[key]))
    return jobs
