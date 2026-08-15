# 占位符保护：翻译前把 MC 格式码/占位符替换成安全标记（防 AI 翻坏），翻译后还原
import re

# 覆盖：
#   - MC 格式码 §a/&a
#   - printf 完整格式说明符 %s / %1$s / %5.2f / %-10s / %+,.2f / %%
#   - Minecraft 命令/路径 token（/give @p diamond、config/jei/jei.toml、modid:key）
#   - 颜色 #FFF/#FFFFFF、{var}/{0}、<item:...>、{{...}}、\n
#
# / 分支要点（Xaero 审查修复：禁止「/token + 空格词组」贪婪吞掉英文句子）：
#   world/server. You can read... 里的 /server 只是普通斜杠分隔（world 与 server 并列），
#   不是程序标识——若被 protect 成占位符，模型只翻 world、restore 贴回英文 → 中英混杂。
#   只保护「真标识」：含扩展名（file.json）、多级路径（a/b/c）、注册名（modid:key）、
#   命令（/give @p diamond，要求 / 前非字母数字）。
_PLACEHOLDER_RE = re.compile(
    r"§x(?:§[0-9a-fA-F]){6}"          # §x RGB 扩展格式码（1.16+，§x§R§G§B§A 六段，修复：漏保护 AI 会翻坏格式码）
    r"|&x(?:&[0-9a-fA-F]){6}"         # &x RGB 扩展格式码（&x&R&G&B&A 六段，修复 recheck：原被 &a 拆成
                                      # 7 个独立 token，AI 重排后 restore 错位颜色码乱掉）
    r"|§[0-9a-fk-or]"                 # MC 格式码 §a
    r"|&[0-9a-fk-or]"                 # & 格式码
    # 修复（recheck）：printf 标志位不含空格——之前 [-#+ 0,(]* 含空格会把「50% off」误保护成
    # 碎片（50%%MC_0%%ff），AI 看到碎片可能删改占位符 → validate 判失败。移除空格。
    r"|%(?:\d+\$)?[-#+0,()]*\d*(?:\.\d+)?[a-zA-Z%]"  # printf 完整格式：%s %1$s %5.2f %-10s %+,.2f %%
    r"|(?<![:/])/[A-Za-z0-9_@-]+(?:\.[A-Za-z0-9_-]+)+"       # 含扩展名文件路径：/path/file.json、a/b.tar.gz
    r"|(?<![:/])/[A-Za-z0-9_@.-]+:[A-Za-z0-9_./@-]+"          # 注册名路径：/modid:key
    r"|(?<![:/])/[A-Za-z0-9_@.-]+(?:/[A-Za-z0-9_@.-]+)+"      # 多级路径（≥2 段 /）：config/jei/jei.toml
    r"|(?<![A-Za-z0-9_])/[a-z][a-z0-9-]+(?:\s+[A-Za-z0-9_@:./{}\[\]<>+-]+)*"  # 命令：/give @p diamond
    # v1.3.5（用户「all Matter Energy→石"+technology"+" 乱码」）：Markdown **加粗**标记
    # 单独保护——AI 把 `**` 输出成 `"+"` 破坏结构；保护后 AI 翻中间文本、restore 还原 `**`
    #（mc_translator 格式保护思路）。链接 [text](url)/图片 ![alt](url) 由 prompt markdown
    # 规则（保留语法、只翻文字）处理。
    r"|\*\*"
    r"|#(?:[0-9a-fA-F]{3}){1,2}"      # #FFF / #FFFFFF
    r"|\{[^{}]*\}"                    # {var} / {0}
    r"|<[^<>]*>"                      # <item:iron_ingot>
    r"|\{\{.*?\}\}"                   # {{...}}
    r"|\\n"
)

_MARK_RE = re.compile(r"%%MC_(\d+)%%")

# 哨兵前缀：原文已存在的 %%MC_n%% 字样临时占位用（私人用区字符，_MARK_RE 不匹配）
_PRE_SENTINEL = ""


def extract_tokens(text: str) -> list[str]:
    """提取文本中的占位符 token 列表（按出现顺序，含重复，供 validate 比对）。"""
    return _PLACEHOLDER_RE.findall(text)


def protect(text: str) -> tuple[str, list[str]]:
    """把占位符替换成 %%MC_i%% 标记，返回 (脱敏文本, 原始标记列表)。"""
    markers: list[str] = []
    # 防误还原（修复）：原文若本身就含 %%MC_n%% 字样，直接留给 _PLACEHOLDER_RE
    # 会被拆成两个「%%」分别收编，restore 时被误还原成其他占位符（污染译文）。
    # 先整体换成哨兵，sub 完再收编进 markers（restore 还原成原文的 %%MC_n%%）。
    pre: list[str] = []

    def _esc(m: re.Match) -> str:
        pre.append(m.group(0))
        return f"{_PRE_SENTINEL}{len(pre) - 1}::"

    text = _MARK_RE.sub(_esc, text)

    def _repl(m: re.Match) -> str:
        markers.append(m.group(0))
        return f"%%MC_{len(markers) - 1}%%"

    masked = _PLACEHOLDER_RE.sub(_repl, text)
    for k, orig in enumerate(pre):
        masked = masked.replace(f"{_PRE_SENTINEL}{k}::", f"%%MC_{len(markers)}%%")
        markers.append(orig)
    return masked, markers


def clean_surrogates(text: str) -> str:
    """移除/替换无效 surrogate 码点（U+D800-DFFF）。

    LLM 输出或转码可能产生 surrogate，ensure_ascii=False 的 json.dumps 会把它原样保留，
    write_text(utf-8) 编码时抛 "surrogates not allowed" 崩溃（用户实测翻译 2 小时炸）。
    用 U+FFFD 替换保长度，不影响正常字符；无 surrogate 时零开销直接返回。
    """
    if not text:
        return text
    if not any(0xD800 <= ord(c) <= 0xDFFF for c in text):
        return text
    return text.encode("utf-8", errors="replace").decode("utf-8")


def restore(masked: str, markers: list[str]) -> str:
    """把 %%MC_i%% 还原回原始占位符；索引越界的标记原样保留。"""
    def _repl(m: re.Match) -> str:
        idx = int(m.group(1))
        if 0 <= idx < len(markers):
            return markers[idx]
        return m.group(0)

    return _MARK_RE.sub(_repl, masked)


def validate(source: str, translation: str) -> bool:
    """占位符一致性校验：源与译文的占位符 token 数量+内容逐一相等（忽略顺序）。

    返回 True 表示译文完整保留了源的占位符；False 表示有丢失/改写/新增
    （供调用方标记 failed 或重试）。

    换行符（\\n）不参与校验（用户反馈：原文换行 vs 译文 \\n 转义差异是正常的，
    内容完整即可，不应因换行符判占位符丢失）。
    """
    st = [t for t in extract_tokens(source) if t != "\\n"]
    tt = [t for t in extract_tokens(translation) if t != "\\n"]
    return sorted(st) == sorted(tt)
