"""错误消息脱敏：任务异常 str(e) 常带本机绝对路径（PermissionError 文件路径、
WindowsError 完整路径等），直接推给前端会泄露用户目录结构。统一把常见本机路径前缀
替换为占位符，仅保留关键信息（文件名等由调用方自行截取）。"""
import re
import tempfile
from pathlib import Path


def _dirs() -> list[str]:
    out = []
    for d in (tempfile.gettempdir(), str(Path.home())):
        if d:
            out.append(d.replace("\\", "/"))
    return out


_PATTERNS: list[re.Pattern] | None = None


def sanitize_error(msg: str) -> str:
    """把消息里的本机绝对路径前缀替换为「<本机路径>」占位符。"""
    global _PATTERNS
    if _PATTERNS is None:
        _PATTERNS = [re.compile(re.escape(d)) for d in _dirs()]
    out = msg
    for pat in _PATTERNS:
        out = pat.sub("<本机路径>", out)
    return out
