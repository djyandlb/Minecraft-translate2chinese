import re

# 技术串跳过滤：UUID/纯字母/标识符形态不翻译
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
# 纯字母单词（如 hi）→ 保留；snake_case/含数字的标识符（如 iron_ingot）→ 跳过
_PURE_ALPHA_RE = re.compile(r"^[a-zA-Z]+$")
_IDENT_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
# 有实际含义的字符：拉丁字母或汉字（纯数字/符号串如 "123" 不含，跳过）
# v1.2.8 修复（recheck）：补假名/西里尔/谚文/希腊/泰文——非拉丁源（日文/俄文/韩文）的
# json/lines/硬编码文本不再被 needs_translation 全部跳过静默漏翻
_HAS_MEANING_RE = re.compile(
    r"[a-zA-Z一-鿿㐀-䶿ぁ-んァ-ヶ"
    r"Ѐ-ӿ가-힯Α-ω฀-๿]")
# 带点无空格（类路径/域名/版本号/带点标识符）：com.example.Mod / path.to.x / v1.2.3。
# 用户判定「中间带这么多点又不是句号怎么可能是文本」——句号后跟空格才是句子
# （Hello. This is...）；点后无空格是代码标识 → 跳过。
_DOTTED_IDENT_RE = re.compile(r"\.[a-zA-Z0-9_]")
_SENTENCE_DOT_RE = re.compile(r"\.\s")


def should_translate(text: str, max_len: int = 1000) -> bool:
    """判断一段文本是否值得翻译（技术串/标识符/路径/纯数字/UUID/命名空间跳过）。

    max_len：长度上限（默认 1000）。明确文本载体（Patchouli 教程书条目 text/advancements
    description）可放宽到 5000——修复 recheck：长教程文本（>1000 字符）被当超长漏提。
    """
    if not (2 <= len(text) <= max_len):
        return False
    # 纯符号/数字串（无字母、无汉字）→ 跳过，如 "123"
    if not _HAS_MEANING_RE.search(text):
        return False
    # UUID → 跳过
    if _UUID_RE.match(text):
        return False
    # 路径/文件名（含 / 或 \）：
    #   - 含扩展名（含点）→ 跳过，如 "mods/demo/foo.class"、"config/jei/jei.toml"
    #   - 纯小写无空格（config/jei、world/server 单独）→ 路径/标识跳过
    #   - 含大写（Raining/Snowing 天气选项）或含空格（world/server 句子）→ 是斜杠并列词
    #     或句子，必须翻译（Xaero "Raining/Snowing"、"world/server to teleport" 漏翻实测）
    if "/" in text or "\\" in text:
        if "." in text:
            return False
        if not re.search(r"[A-Z]", text) and " " not in text:
            return False
    # 以 / @ # [ 开头的技术串 → 跳过
    if text.startswith(("/", "@", "#", "[")):
        return False
    # 命名空间/键值串（含冒号且无空格）→ 跳过，如 "abc:def"
    if ":" in text and " " not in text:
        return False
    # 带点无空格（类路径/域名/版本号/带点标识符）→ 跳过，如 "com.example.Mod"、
    # "path.to.model"、"v1.2.3"。句号后跟空格才是句子（"Hello. This is..." → 翻译）。
    # v1.3.0 修复（用户「含版本号的句子全英文」）：**含空格的句子即使带点也不跳过**——
    # "Welcome to Project Infinity 0.1" 的版本号 ".1" 会被 _DOTTED_IDENT_RE 命中误跳过；
    # 只有无空格的纯标识符形态（com.example.Mod / 1.2.3）才是代码/版本，跳过。
    if "." in text and " " not in text and _DOTTED_IDENT_RE.search(text):
        return False
    # 短编码（≤6 全大写+数字，如 BB/B0PB）→ 代码/键码跳过（对照 mc_translator skip_rules）
    if len(text) <= 6 and re.fullmatch(r"[A-Z0-9]+", text):
        return False
    # Base64（≥8 无空格 以 = 结尾）→ 跳过
    if len(text) >= 8 and " " not in text and text.endswith("="):
        return False
    # hex 颜色码（长度 6/8 全十六进制，如 ff00ff）→ 跳过
    if len(text) in (6, 8) and re.fullmatch(r"[0-9a-fA-F]+", text):
        return False
    # 纯字母单词（如 "hi"/"Bombs"/"Plantkillable"）→ 保留（用户可见文本，该翻译；
    # 代码标识（驼峰类名等）由硬编码 AI 裁判把关，语言文件 value 都是用户可见文本）
    if _PURE_ALPHA_RE.match(text):
        return True
    # 技术标识符（snake_case / 含数字变量名）→ 跳过，如 "iron_ingot"
    if _IDENT_RE.match(text):
        return False
    return True
