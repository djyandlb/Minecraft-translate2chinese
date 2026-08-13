# -*- coding: utf-8 -*-
"""官方术语质量审计：对语言文件译文逐条检查 MC 官方简中术语 / 占位符一致性 / 键名语义。

机械交付不变量为主：语言选择属于证据而非硬性路线指令——所以这里只给
「必须保留官方术语/材料语义/占位符」的强规则（error）与「建议结合模型确认」的软规则（warning）。
"""
import re
from collections import defaultdict

from app.placeholder import validate

# 官方简中术语规则：(源英文正则, 必需中文词, 违规说明)。19 条具体术语。
_OFFICIAL_TERMS = [
    (r"\bMacaw(?:'s)?\b", "Macaw", "模组品牌 Macaw 应保留原文，不应擅自音译"),
    (r"\bAdditions\b", "扩展", "Additions 应译为自然的「扩展」"),
    (r"\bIron Bars\b", "铁栏杆", "Iron Bars 应沿用 Minecraft 官方简中术语「铁栏杆」"),
    (r"\bResizeable\b", "可变形", "Resizeable 在可改变外形的窗户语境中应译为「可变形」"),
    (r"\bEnd(?:er)? Brick\b", "末地石砖", "End Brick 应沿用 Minecraft 官方材料名「末地石砖」"),
    (r"\bCrimson\b", "绯红", "Crimson 应沿用「绯红」"),
    (r"\bWarped\b", "诡异", "Warped 应沿用「诡异」"),
    (r"\bPale Oak\b", "苍白橡木", "Pale Oak 应沿用「苍白橡木」"),
    (r"\bDark Oak\b", "深色橡木", "Dark Oak 应沿用「深色橡木」"),
    (r"\bOak Planks\b", "橡木木板", "Oak Planks 应沿用完整材料名「橡木木板」"),
    (r"\bDark Oak Planks\b", "深色橡木木板", "Dark Oak Planks 应沿用完整材料名「深色橡木木板」"),
    (r"\bSpruce Planks\b", "云杉木板", "Spruce Planks 应沿用「云杉木板」"),
    (r"\bBirch Planks\b", "白桦木板", "Birch Planks 应沿用「白桦木板」"),
    (r"\bJungle Planks\b", "丛林木板", "Jungle Planks 应沿用「丛林木板」"),
    (r"\bAcacia Planks\b", "金合欢木板", "Acacia Planks 应沿用「金合欢木板」"),
    (r"\bCherry Planks\b", "樱花木板", "Cherry Planks 应沿用「樱花木板」"),
    (r"\bMangrove Planks\b", "红树木板", "Mangrove Planks 应沿用「红树木板」"),
    (r"\bCrimson Planks\b", "绯红木板", "Crimson Planks 应沿用「绯红木板」"),
    (r"\bWarped Planks\b", "诡异木板", "Warped Planks 应沿用「诡异木板」"),
]

# 材质语序规则：(源英文正则, 材质中文词)。用于「材质 + 玻璃板 + 窗」自然语序校验。
_MATERIAL_ORDER = [
    (r"\bDark Oak\b", "深色橡木"),
    (r"\bPale Oak\b", "苍白橡木"),
    (r"\bOak\b", "橡木"),
    (r"\bSpruce\b", "云杉"),
    (r"\bBirch\b", "白桦"),
    (r"\bJungle\b", "丛林"),
    (r"\bAcacia\b", "金合欢"),
    (r"\bCherry\b", "樱花"),
    (r"\bMangrove\b", "红树"),
]


def _err(key: str, message: str) -> dict:
    """构造 error 级问题条目。"""
    return {"severity": "error", "key": key, "message": message}


def _warn(key: str, message: str) -> dict:
    """构造 warning 级问题条目。"""
    return {"severity": "warning", "key": key, "message": message}


def _audit_key_semantics(key: str, chinese: str, errors: list, warnings: list) -> None:
    """键名语义审计（不依赖源文本）：open/close 动作、_log 原木语义。"""
    if re.search(r"(?:^|[._])open$", key, re.I) and not re.search(r"(?:打开|开启)", chinese):
        errors.append(_err(key, "键名表示打开动作，译文必须与关闭动作明确区分"))
    if re.search(r"(?:^|[._])close$", key, re.I) and not re.search(r"(?:关闭|合上)", chinese):
        errors.append(_err(key, "键名表示关闭动作，译文必须与打开动作明确区分"))
    # 修复（recheck）：log 是**原木**材料语义只在该键确实指物品时成立——debug_log/error_log/
    # enable_log 等日志语境键会被误判「必须含原木」→ 真实译文「调试日志」被报 error 触发强制重翻。
    # 排除日志语境前缀后，仅独立 log / 物品相关键才要求「原木」。
    _log_key = re.search(r"(?:^|_)(log)(?:_|$)", key, re.I)
    if (_log_key
            and not re.search(r"(?:debug|error|chat|console|warn|info|enable|disable|level|file|"
                              r"server|client|open|close|show|hide)_log", key, re.I)
            and "原木" not in chinese):
        errors.append(_err(key, "键名含 log 表明是原木版本，译名必须保留「原木」"))


def _audit_with_source(key: str, english: str, chinese: str,
                       errors: list, warnings: list) -> None:
    """有源文本时的审计：占位符一致性 + 官方术语 + 材料语义 + 语序。"""
    # 占位符一致性：源 token 与译文 token 逐一相等
    if not validate(english, chinese):
        errors.append(_err(key, "译文丢失或改写了占位符，与源文本不一致"))

    # 官方术语硬规则
    for pat, needed, label in _OFFICIAL_TERMS:
        if re.search(pat, english, re.I) and needed not in chinese:
            errors.append(_err(key, label))

    # 通用材料语义：Planks/Stem 必须保留材料含义
    if re.search(r"\bPlanks\b", english, re.I) and "木板" not in chinese:
        errors.append(_err(key, "Planks 必须保留「木板」材料含义"))
    if re.search(r"\bStem\b", english, re.I) and "菌柄" not in chinese:
        errors.append(_err(key, "Stem 必须保留「菌柄」材料含义"))

    # Log/Timber/Wood 材料语义（排除 Journal/Logbook 及日志语境等非材料含义）。
    # 修复（recheck）："Debug Log"/"Chat log"/"Console Log"/"Log file" 等日志 UI 文本
    # 之前被 \bLog\b 命中要求「原木」→ 真实译文「调试日志」被误报 error；排除表扩展日志语境。
    # 另去掉 Stem（84 行单独要求「菌柄」，与「原木」矛盾——Stem 译文不含原木会被这里误报）。
    if (re.search(r"\b(?:Log|Timber|Wood)\b", english, re.I)
            and not re.search(r"\b(?:Journal|Logbook|Research Log|Data Log|Debug Log|Chat Log|"
                              r"Console Log|Log File|Log Output|Server Log|Error Log|Game Log|"
                              r"Logs)\b", english, re.I)
            and "原木" not in chinese):
        errors.append(_err(key, "原文含 Log/Timber/Wood 材料语义，译名必须保留「原木」"))

    # Pane Window 玻璃板（软规则，需结合模型确认）
    if (re.search(r"\b(?:Four )?Pane Window\b", english, re.I)
            and "玻璃板" not in chinese
            and not re.search(r"\bFour Pane\b", english, re.I)):
        warnings.append(_warn(key, "Pane Window 通常应体现「玻璃板」；请结合模型确认"))

    # Pane Window 语序：材质 + 玻璃板 + 窗
    if re.search(r"\bPane Window\b", english, re.I) and "玻璃板" in chinese:
        material = next((m for pat, m in _MATERIAL_ORDER if re.search(pat, english, re.I)), None)
        # 修复：material 未必出现在 AI 译文中（可能漏译材质词），必须先在 chinese 判存在，
        # 否则 chinese.index 抛 ValueError → 冒泡炸掉整个翻译任务
        if material and material in chinese and chinese.index(material) > chinese.index("玻璃板"):
            errors.append(_err(key, "物品名应采用「材质 + 玻璃板 + 窗」的自然语序"))

    # Window Base 底座（软规则）
    if (re.search(r"\bWindow (?:Four |Half )?Pane Base\b|\bWindow Base\b", english, re.I)
            and "底座" not in chinese):
        warnings.append(_warn(key, "作为合成组件的 Base 通常译为「底座」比「基础」自然"))


def audit_invariants(source: dict[str, str], target: dict[str, str]) -> list[dict]:
    """机械交付不变量审计：缺少译文（error）/ 占位符不一致（error）/ 额外条目（warning）。

    对标主流汉化工具的不变量审计：不变量是硬性交付要求，**error 级 key
    作为 hardKeys 由调用方强制重翻**（语言文件阶段审计→重翻闭环用），而非仅提示。
    语言选择不属于此处（术语/语义由 audit_translation 负责）。
    """
    issues: list[dict] = []
    for key, english in source.items():
        chinese = target.get(key)
        if not isinstance(chinese, str) or not chinese.strip():
            issues.append(_err(key, "缺少中文译文"))
            continue
        if not validate(english, chinese):
            issues.append(_err(key, "译文丢失或改写了占位符，与源文本不一致"))
    for key in target:
        if key not in source:
            issues.append(_warn(key, "目标语言文件包含源语言不存在的额外条目"))
    return issues


def audit_translation(by_mod: dict[str, dict[str, str]], target: str,
                      source_by_mod: dict[str, dict[str, str]] | None = None) -> tuple[list, list]:
    """逐条审计语言文件译文，返回 (errors, warnings)。

    by_mod: {modid: {key: 译文}}——目标语言译文条目。
    target: target_lang（zh_cn/zh_tw 等；本版审计规则不依赖，保留参数对齐调用方）。
    source_by_mod: {modid: {key: 源文本}}——可选的源语言条目；缺省时跳过依赖
        源文本的规则（占位符一致性、官方术语、材料语义），仅做键名语义审计。

    返回的 errors/warnings 每条为 {"severity", "key", "message"}。
    """
    errors: list[dict] = []
    warnings: list[dict] = []

    for modid, entries in by_mod.items():
        source_entries = (source_by_mod or {}).get(modid, {})
        for key, chinese in entries.items():
            if not isinstance(chinese, str) or not chinese.strip():
                errors.append(_err(key, "缺少中文译文"))
                continue
            english = source_entries.get(key)
            if english is not None:
                _audit_with_source(key, english, chinese, errors, warnings)
            _audit_key_semantics(key, chinese, errors, warnings)
            # 助词冗余机械检查（系统性修复「符文的的宝珠」）：连续重复格助词 → warning。
            # 先移除白名单成语（的的确确/了了分明/地地道道）避免误报合法文本；
            # 字符集不含「得」——「得得」多是拟声词（马蹄声得得）合法（Agent 审查）。
            _chk = (chinese.replace("的的确确", "").replace("了了分明", "")
                    .replace("地地道道", ""))
            if re.search(r"([的地之了])\1", _chk):
                warnings.append(_warn(key, "译文含连续重复格助词（如「的的」），应合并为一个助词"))

    # 重复译法：相同英文原文出现多种译法 → warning（除非 open/close 成对动作键）
    if source_by_mod:
        by_english: dict[str, list] = defaultdict(list)
        for modid, source_entries in source_by_mod.items():
            entries = by_mod.get(modid, {})
            for key, english in source_entries.items():
                if key in entries:
                    by_english[english].append({"key": key, "chinese": entries[key]})
        for english, group in by_english.items():
            if len(group) < 2:
                continue
            variants = {g["chinese"] for g in group}
            has_action_pair = (any(re.search(r"(?:^|[._])open$", g["key"], re.I) for g in group)
                               and any(re.search(r"(?:^|[._])close$", g["key"], re.I) for g in group))
            if len(variants) > 1 and not has_action_pair:
                for g in group:
                    warnings.append(_warn(
                        g["key"],
                        f"相同英文原文「{english}」出现 {len(variants)} 种译法，请确认是否需要统一"))

    return errors, warnings
