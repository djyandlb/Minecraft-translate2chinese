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
        # v1.3.9 修复（用户「AE2 教程 markdown 没翻译」）：markdown 链接
        # `[network's storage](.../ae2-mechanics/import-export-storage.md)` 含 / 和 . 被当
        # 路径跳过 → 教程正文漏翻。只有**整行是纯路径/文件名形态**（无空格、每段是
        # 标识符）才跳过；markdown 链接（含 []() 标记或空格）是文本，必须翻译。
        if re.fullmatch(r"(?:[A-Za-z0-9_@.-]+/)+[A-Za-z0-9_@.-]+", text.strip()):
            return False                                   # 纯路径 config/jei/jei.toml
        if "." in text and " " not in text:
            return False                                   # 含扩展名无空格文件路径
        if not re.search(r"[A-Z]", text) and " " not in text:
            return False
    # 以 / @ # [ 开头的技术串 → 跳过，但**语义判断**（v1.3.9 修复用户「AE2 教程没翻译」）：
    # markdown 标题（# Wireless Terminal）、链接（[network's storage](url)）是用户可见
    # 教程文本，不能因首字符跳过。只跳真正的技术指令：
    #   - #version/#define/#include 等 shader/预处理指令（# 后无空格直接字母）
    #   - /give /teleport 等命令（/ 后无空格）
    #   - @param/@return 等 Javadoc 注解
    #   - [i0] 行号标签、[内容] 代码下标
    # markdown 标题「# 空格 + 单词」、链接「[文字](url)」（含空格或括号）是文本，翻译。
    if text.startswith("#") and not re.match(r"^#+\s", text):
        return False
    if text.startswith("/") and not text.startswith("/ "):
        return False
    if text.startswith("@") and not text.startswith("@ "):
        return False
    if text.startswith("["):
        # markdown 链接 [text](url) → 用户可见文本，翻译（AE2 教程链接文字）。
        # [path.md] 后跟正文（含空格）→ 也是教程文本，翻译。
        # [i0] / [iN] 行号标签前缀（[ 后紧跟 i数字]）→ 代码标注，即使后面有文字也跳过
        if re.match(r"^\[i\d+\]", text):
            return False
        # 纯代码下标 [arr[0]] / 纯路径链接 [path.md]（无 ]( 且无空格）→ 技术串跳过
        if re.search(r"\]\(", text):
            return True
        if " " in text:
            return True
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
    # 纯字母单词：区分「普通英文单词（该翻）」和「代码标识/结构串（不该翻）」。
    # - 全大写（≥4字符）：PCPPPPPCP、HHHHHHHHH、ABCDEF → 代码/模式串，跳过
    # - 驼峰（小写后跟大写）：ModelViewMat、texCoord、Brightness → 代码标识，跳过
    # - 首字母大写普通单词：Bombs、Enable、Sprint → 用户可见文本，翻译
    # - 全小写：enable、sprint → 用户可见文本，翻译
    if _PURE_ALPHA_RE.match(text):
        if len(text) >= 4 and text == text.upper():
            return False                      # 全大写 ≥4：代码/模式串
        if re.search(r"[a-z][A-Z]", text):
            return False                      # 驼峰：代码标识（ModelViewMat/texCoord）
        return True                           # 普通单词：该翻译
    # 技术标识符（snake_case / 含数字变量名）→ 跳过，如 "iron_ingot"
    if _IDENT_RE.match(text):
        return False
    return True
