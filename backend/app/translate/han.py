from opencc import OpenCC

# V3 决策：zh_cn↔zh_tw 简繁直转，免 AI 烧钱
_t2s = OpenCC("t2s")   # 繁→简
_s2t = OpenCC("s2t")   # 简→繁

def simplify(text: str) -> str:
    return _t2s.convert(text)

def traditional(text: str) -> str:
    return _s2t.convert(text)

def is_same_script(src: str, tgt: str) -> bool:
    """源/目标同为 zh_cn/zh_tw 时走简繁直转（跳过 AI）。"""
    return {src, tgt} <= {"zh_cn", "zh_tw"}
