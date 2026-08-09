# MC 版本 → 资源包格式版本（pack_format）已知映射
_KNOWN: dict[str, int] = {
    "1.12.2": 4, "1.13.2": 4, "1.14.4": 4, "1.15.2": 5,
    "1.16.5": 6, "1.17.1": 7, "1.18.2": 9, "1.19.2": 12,
    "1.20.1": 15, "1.20.4": 22, "1.21": 34, "1.21.4": 46,
    "1.21.5": 55,
}

def version_to_pack_format(version: str) -> int:
    """MC 版本字符串 → pack_format；未知版本回退 15（1.20.1）。"""
    return _KNOWN.get(version, 15)

def pack_format_to_lang_ext(pack_format: int) -> str:
    """pack_format ≥ 4（1.13+）用 .json；1.12 及以下用 .lang。"""
    return "json" if pack_format >= 4 else "lang"
