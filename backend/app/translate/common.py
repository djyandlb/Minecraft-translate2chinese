import re

# 借鉴 mc_translator(skip_rules.rs) 的跳过滤思想
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
# 纯字母单词（如 hi）→ 保留；snake_case/含数字的标识符（如 iron_ingot）→ 跳过
_PURE_ALPHA_RE = re.compile(r"^[a-zA-Z]+$")
_IDENT_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
# 有实际含义的字符：拉丁字母或汉字（纯数字/符号串如 "123" 不含，跳过）
_HAS_MEANING_RE = re.compile(r"[a-zA-Z一-鿿]")


def should_translate(text: str) -> bool:
    """判断一段文本是否值得翻译（技术串/标识符/路径/纯数字/UUID/命名空间跳过）。"""
    if not (2 <= len(text) <= 1000):
        return False
    # 纯符号/数字串（无字母、无汉字）→ 跳过，如 "123"
    if not _HAS_MEANING_RE.search(text):
        return False
    # UUID → 跳过
    if _UUID_RE.match(text):
        return False
    # 路径（含 / 或 \）→ 跳过，如 "mods/demo/foo.class"
    if "/" in text or "\\" in text:
        return False
    # 以 / @ # [ 开头的技术串 → 跳过
    if text.startswith(("/", "@", "#", "[")):
        return False
    # 命名空间/键值串（含冒号且无空格）→ 跳过，如 "abc:def"
    if ":" in text and " " not in text:
        return False
    # 纯字母单词（如 "hi"）→ 保留
    if _PURE_ALPHA_RE.match(text):
        return True
    # 技术标识符（snake_case / 含数字变量名）→ 跳过，如 "iron_ingot"
    if _IDENT_RE.match(text):
        return False
    return True
