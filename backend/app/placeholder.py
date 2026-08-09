# 占位符保护：翻译前把 MC 格式码/占位符替换成安全标记（防 AI 翻坏），翻译后还原
import re

# 借鉴 mc_translator(text_processing.rs) 占位符保护思想 + bfy-study placeholders.js 增强，Python 化实现。
# 覆盖：
#   - MC 格式码 §a/&a
#   - printf 完整格式说明符 %s / %1$s / %5.2f / %-10s / %+,.2f / %%
#   - Minecraft 命令/路径 token（/give @p diamond、config/jei/...）
#   - 颜色 #FFF/#FFFFFF、{var}/{0}、<item:...>、{{...}}、\n
_PLACEHOLDER_RE = re.compile(
    r"§[0-9a-fk-or]"                  # MC 格式码 §a
    r"|&[0-9a-fk-or]"                 # & 格式码
    r"|%(?:\d+\$)?[-#+ 0,(]*\d*(?:\.\d+)?[a-zA-Z%]"  # printf 完整格式：%s %1$s %5.2f %-10s %+,.2f %%
    r"|(?<![:/])/[A-Za-z0-9_.:@-]+(?:\s+(?:[A-Za-z0-9_.:@-]+|\{[^{}\s]+\}|<[^<>\s]+>|\[[^\[\]\s]+\]))*"  # MC 命令/路径 token
    r"|#(?:[0-9a-fA-F]{3}){1,2}"      # #FFF / #FFFFFF
    r"|\{[^{}]*\}"                    # {var} / {0}
    r"|<[^<>]*>"                      # <item:iron_ingot>
    r"|\{\{.*?\}\}"                   # {{...}}
    r"|\\n"
)

_MARK_RE = re.compile(r"%%MC_(\d+)%%")


def extract_tokens(text: str) -> list[str]:
    """提取文本中的占位符 token 列表（按出现顺序，含重复，供 validate 比对）。"""
    return _PLACEHOLDER_RE.findall(text)


def protect(text: str) -> tuple[str, list[str]]:
    """把占位符替换成 %%MC_i%% 标记，返回 (脱敏文本, 原始标记列表)。"""
    markers: list[str] = []

    def _repl(m: re.Match) -> str:
        markers.append(m.group(0))
        return f"%%MC_{len(markers) - 1}%%"

    return _PLACEHOLDER_RE.sub(_repl, text), markers


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
    """
    return sorted(extract_tokens(source)) == sorted(extract_tokens(translation))
