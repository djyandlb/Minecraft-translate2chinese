"""错误消息脱敏：任务异常 str(e) 常带本机绝对路径（PermissionError 文件路径、
WindowsError 完整路径等），直接推给前端会泄露用户目录结构。统一把常见本机路径前缀
替换为占位符，仅保留关键信息（文件名等由调用方自行截取）。"""
import re
import tempfile
from pathlib import Path


def _dirs() -> list[str]:
    """返回本机路径前缀（正斜杠 + 反斜杠两个变体）。
    修复（recheck）：Windows 异常消息（PermissionError 等）用反斜杠路径，
    只生成正斜杠变体无法匹配 → 脱敏失效、本机目录结构泄露给前端。
    v1.2.8 修复：追加 cwd——用户自定义 cache_dir 不在 temp/home 下时（如 D:\\MyCache），
    异常消息里的该路径前缀也能被替换，不泄露自定义目录结构。"""
    out = []
    for d in (tempfile.gettempdir(), str(Path.home()), str(Path.cwd())):
        if d:
            out.append(d.replace("\\", "/"))
            out.append(d.replace("/", "\\"))
    return out


_PATTERNS: list[re.Pattern] | None = None


def sanitize_error(msg: str) -> str:
    """把消息里的本机绝对路径前缀替换为「<本机路径>」占位符。"""
    global _PATTERNS
    if _PATTERNS is None:
        _PATTERNS = [re.compile(re.escape(d), re.IGNORECASE) for d in _dirs()]
    out = msg
    for pat in _PATTERNS:
        out = pat.sub("<本机路径>", out)
    return out
