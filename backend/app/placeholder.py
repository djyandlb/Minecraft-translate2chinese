# 占位符保护：翻译前把 MC 格式码/占位符替换成安全标记（防 AI 翻坏），翻译后还原
import re

# 借鉴 mc_translator(text_processing.rs) 占位符保护思想，Python 化实现。
# 覆盖：MC 格式码 §a/&a、%s/%1$s/%%、颜色 #FFF/#FFFFFF、{var}/{0}、<item:...>、{{...}}、\n
_PLACEHOLDER_RE = re.compile(
    r"§[0-9a-fk-or]"                  # MC 格式码 §a
    r"|&[0-9a-fk-or]"                 # & 格式码
    r"|%(\d+\$)?[a-zA-Z%]"            # %s %1$s %%
    r"|#(?:[0-9a-fA-F]{3}){1,2}"      # #FFF / #FFFFFF
    r"|\{[^{}]*\}"                    # {var} / {0}
    r"|<[^<>]*>"                      # <item:iron_ingot>
    r"|\{\{.*?\}\}"                   # {{...}}
    r"|\\n"
)

_MARK_RE = re.compile(r"%%MC_(\d+)%%")


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
