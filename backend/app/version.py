# MC 版本 → 资源包格式版本（pack_format）已知映射。
# 资源包 pack_format 严格对应 MC 版本：写错会被对应版本游戏拒绝加载（材质包不兼容，
# 用户实测）。权威对照：Minecraft Wiki Pack_format / NyaaCat/ROTA 表 / Nixinova/pack-format。
# 表覆盖 1.6.1 ~ 1.21.10 主流版本；未知版本由 version_to_pack_format 前缀匹配 + 默认
# 15（1.20.1）兜底。
#
# 注意：1.20.3 起资源包与数据包 pack_format **分开计数**——本表是**资源包**的（模组/整合包
# 汉化产物是资源包，不是数据包），勿与数据包格式混淆（1.20.4 资源包 22、数据包 26）。
_KNOWN: dict[str, int] = {
    # 1.6.1-1.8.9 → 1（1.7.10 是经典模组版本，写错会拒载）
    "1.6.1": 1, "1.6.2": 1, "1.6.4": 1, "1.7": 1, "1.7.2": 1, "1.7.9": 1, "1.7.10": 1,
    "1.8": 1, "1.8.8": 1, "1.8.9": 1,
    # 1.9-1.10.2 → 2
    "1.9": 2, "1.9.4": 2, "1.10": 2, "1.10.2": 2,
    # 1.11-1.12.2 → 3
    "1.11": 3, "1.11.2": 3, "1.12": 3, "1.12.2": 3,
    "1.13": 4, "1.13.2": 4,
    "1.14": 4, "1.14.4": 4,
    "1.15": 5, "1.15.2": 5,
    "1.16": 5, "1.16.1": 5, "1.16.2": 6, "1.16.5": 6,
    "1.17": 7, "1.17.1": 7,
    "1.18": 8, "1.18.1": 8, "1.18.2": 8,
    "1.19": 9, "1.19.1": 9, "1.19.2": 9, "1.19.3": 12, "1.19.4": 13,
    "1.20": 15, "1.20.1": 15, "1.20.2": 18, "1.20.3": 22, "1.20.4": 22,
    "1.20.5": 32, "1.20.6": 32,
    "1.21": 34, "1.21.1": 34, "1.21.2": 42, "1.21.3": 42, "1.21.4": 46,
    "1.21.5": 55, "1.21.6": 63, "1.21.7": 64, "1.21.8": 64,
}
# 1.21.9 起（25w31a 快照）pack_format 改为 major.minor 十进制：pack.mcmeta 写数组
# [major, minor]，且 pack_format/supported_formats 字段弃用、改用 min_format/max_format。
# 1.21.9-1.21.10 = 69.0。更高版本（1.21.11+）具体 major 未获权威确认，沿用 69 保守处理
#（detect 命中更高 patch 时按 69.0 写，避免回退 1.20.1 的 15 拒载）。
_NEW_FORMAT_MIN_MAJOR = 69


def _is_new_format(version: str) -> bool:
    """1.21.9+（major.minor 数组格式）判定。"""
    parts = version.split(".")
    return (len(parts) >= 3 and parts[0] == "1" and parts[1] == "21"
            and parts[2].isdigit() and int(parts[2]) >= 9)


def version_to_pack_format(version: str) -> int:
    """MC 版本字符串 → pack_format（整数部分）；未知版本回退 15（1.20.1）。

    精确命中优先；1.21.9+ 返回新格式整数（69）；其次按主版本前缀降级匹配
    （1.20.1 → 1.20 前缀命中；表里只有 patch 版本时同主版共享格式）。空串/未知回退
    15（1.20.1 是当前最主流基线）。
    """
    if not version:
        return 15
    if version in _KNOWN:
        return _KNOWN[version]
    if _is_new_format(version):
        return _NEW_FORMAT_MIN_MAJOR
    parts = version.split(".")
    # 从最长前缀依次降级：1.20.1 → "1.20"；1.20 → 无（兜底 15）
    for n in range(len(parts) - 1, 1, -1):
        v = ".".join(parts[:n])
        if v in _KNOWN:
            return _KNOWN[v]
    return 15


def pack_format_spec(version: str) -> int | list[int]:
    """pack.mcmeta 可序列化的 pack_format：≤1.21.8 返回整数；1.21.9+ 返回 [major, minor]。"""
    if not version:
        return 15
    if _is_new_format(version):
        return [_NEW_FORMAT_MIN_MAJOR, 0]
    return version_to_pack_format(version)


def pack_format_to_lang_ext(pack_format: int) -> str:
    """pack_format ≥ 4（1.13+）用 .json；1.12 及以下用 .lang。"""
    return "json" if pack_format >= 4 else "lang"
