# -*- coding: utf-8 -*-
"""mod 中文名推断与友好产物命名。

优先级链：jar 内已有中文名 → known 表 → 英文名逐词词典翻译（处理 's 所有格）
  → 文件名清洗去版本号 → mod id。
产物命名统一为 {中文名}-汉化版.jar，取不到中文名回退原 jar stem。
"""
import json
import re
import tomllib
import zipfile
from pathlib import Path

# 通用名（无信息量，不作为产物名）
_GENERIC_NAMES = re.compile(
    r"^(?:(?:中文|魔法|冒险|科技|装饰|工具|未知|未命名)?模组|未命名|未知|有趣的冒险|中文扩展|未命名扩展)$"
)

# 已知 mod 中文名表（identity 键：小写 + 去非 [a-z0-9_'] 字符）
_KNOWN_NAMES = {
    "macaw'swindows": "Macaw 的窗户", "mcwwindows": "Macaw 的窗户",
    "iron'sspells'nspellbooks": "铁魔法与法术书", "irons_spellbooks": "铁魔法与法术书",
    "farmer'sdelight": "农夫乐事", "farmersdelight": "农夫乐事",
    "alex'scaves": "Alex 的洞穴", "alexscaves": "Alex 的洞穴",
    "mekanism": "通用机械", "lootmate": "战利品助手",
}

# 英文词 → 中文词典（逐词翻译用，覆盖 mod 名常见词）
_WORD_TRANSLATIONS = {
    "window": "窗户", "windows": "窗户", "cave": "洞穴", "caves": "洞穴",
    "spell": "法术", "spells": "法术", "spellbook": "法术书", "spellbooks": "法术书",
    "loot": "战利品", "mate": "助手", "farmer": "农夫", "farmers": "农夫",
    "delight": "乐事", "magic": "魔法", "iron": "铁", "tools": "工具", "tool": "工具",
    "doors": "门", "door": "门", "planks": "木板", "plank": "木板", "log": "原木",
    "logs": "原木", "crate": "板条箱", "crates": "板条箱", "chest": "箱子", "chests": "箱子",
}


def _identity_key(value: str) -> str:
    """归一化身份键：小写 + 去非 [a-z0-9_'] 字符（对齐 JS identityKey）。"""
    return re.sub(r"[^a-z0-9_']", "", (value or "").lower())


def usable_chinese_name(value) -> bool:
    """验证名字可作为中文 mod 名：含 CJK（\\u3400-\\u9fff）且 ≥2 字且非通用名。"""
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    return (len(normalized) >= 2
            and re.search(r"[㐀-鿿]", normalized) is not None
            and not _GENERIC_NAMES.match(normalized))


def original_project_label(filename: str) -> str | None:
    """文件名清洗去版本号：剥掉 mc1.20.1 / 1.20.1-0.3.5 / -forge 等尾巴。

    返回可作为名字的标签；无信息量文件名（download/mod/file/unknown 等）返回 None。
    """
    stem = Path(filename).name
    if stem.lower().endswith((".jar", ".zip")):
        stem = stem.rsplit(".", 1)[0]
    stem = stem.strip()
    if not stem or re.fullmatch(r"(?:download|mod|file|unknown)(?:[-_ ]?\d+)?", stem, re.I):
        return None
    # 修复（recheck）：开头版本号（1.20.1-MyMod-0.3.5.jar）旧正则要求版本前有分隔符删不掉——
    # 先剥开头版本段，再剥尾部版本段
    without_version = re.sub(
        r"^((?:mc)?\d+\.\d+(?:\.\d+)?(?:[-+._][A-Za-z0-9.]+)*)[-_ ]+", "", stem)
    without_version = re.sub(
        r"[-_ ]+(?:mc)?\d+\.\d+(?:\.\d+)?(?:[-+._][A-Za-z0-9.]+)*.*$", "", without_version).strip()
    label = re.sub(r"[_]+", " ", without_version or stem)
    label = re.sub(r"\s+", " ", label).strip()
    return label or None


def _translate_label(label: str) -> str | None:
    """英文名逐词词典翻译；处理 's 所有格（foo's bar → foo 的 bar）。"""
    clean = re.sub(r"[_\-]+", " ", label).strip()
    clean = re.sub(r"\s+", " ", clean)
    poss = re.match(r"^(.+?)['’]s\s+(.+)$", clean, re.I)
    if poss:
        tail = "".join(_WORD_TRANSLATIONS.get(w.lower(), w) for w in poss.group(2).split())
        if re.search(r"[㐀-鿿]", tail):
            return f"{poss.group(1)} 的{tail}"
    # 修复（recheck）：'s 所有格先统一拆成「的」，再逐词词典翻译——否则 Macaw's Windows 第二段
    # 不在词典时会保留 "Macaw's窗户"（所有格粘连）而非 "Macaw 的窗户"
    clean = re.sub(r"['’]s\s+", " 的 ", clean)
    translated = False
    parts: list[str] = []
    for word in clean.split():
        if re.fullmatch(r"(?:mc|forge|fabric|neoforge|mod)", word, re.I):
            continue
        repl = _WORD_TRANSLATIONS.get(word.lower())
        if repl:
            translated = True
        parts.append(repl or word)
    return "".join(parts) if translated else None


def _jar_metadata(jar: Path) -> tuple[list[str], list[str]]:
    """从 jar 元数据提取 (project_names, mod_ids)。

    读取 fabric.mod.json 的 name/id，及 META-INF/mods.toml、
    META-INF/neoforge.mods.toml 的 displayName/modId；损坏 jar 静默跳过。
    """
    project_names: list[str] = []
    mod_ids: list[str] = []
    try:
        with zipfile.ZipFile(jar) as zf:
            names = zf.namelist()
            if "fabric.mod.json" in names:
                data = json.loads(zf.read("fabric.mod.json").decode("utf-8"))
                if isinstance(data, dict):
                    if isinstance(data.get("name"), str):
                        project_names.append(data["name"])
                    if isinstance(data.get("id"), str):
                        mod_ids.append(data["id"])
            for toml_name in ("META-INF/mods.toml", "META-INF/neoforge.mods.toml"):
                if toml_name not in names:
                    continue
                try:
                    data = tomllib.loads(zf.read(toml_name).decode("utf-8"))
                except Exception:
                    continue
                mods = data.get("mods") if isinstance(data, dict) else None
                if isinstance(mods, list):
                    for m in mods:
                        if not isinstance(m, dict):
                            continue
                        if isinstance(m.get("modId"), str):
                            mod_ids.append(m["modId"])
                        if isinstance(m.get("displayName"), str):
                            project_names.append(m["displayName"])
    except Exception:
        pass
    return project_names, mod_ids


def resolve_mod_name(jar: Path) -> str:
    """推断 mod 中文名（总返回一个名字）。

    优先级：jar 内已有中文名 → known 表 → 英文名逐词词典翻译 →
    文件名清洗去版本号（清洗后仍非中文则返回清洗标签）→ mod id → "mod"。
    """
    project_names, mod_ids = _jar_metadata(jar)

    # 1) jar 内已有中文名
    for name in project_names:
        if usable_chinese_name(name):
            return name.strip()[:64]

    # 2) known 表（mod id / project name 身份匹配）
    for ident in [*mod_ids, *project_names]:
        known = _KNOWN_NAMES.get(_identity_key(ident))
        if known:
            return known

    # 3) 英文显示名逐词词典翻译
    for name in project_names:
        translated = _translate_label(name)
        if usable_chinese_name(translated):
            return translated[:64]

    # 4) 文件名清洗去版本号 → 词典翻译 → 原清洗标签
    label = original_project_label(jar.name)
    if label:
        translated = _translate_label(label)
        if usable_chinese_name(translated):
            return translated[:64]
        return label[:64]

    # 5) mod id 兜底
    for mid in mod_ids:
        if mid:
            return mid[:64]
    return "mod"


# 翻译语言显示名映射：产物命名后缀「{语言}化」跟随用户选择的目标语言。
# zh_cn→简体中文化、zh_tw→繁体中文化、日文→日文化……选中啥语言就命名为啥语言。
_LANG_DISPLAY = {
    "zh_cn": "简体中文",
    "zh_tw": "繁体中文",
    "ja_jp": "日文",
    "ko_kr": "韩文",
    "fr_fr": "法文",
    "de_de": "德文",
    "es_es": "西班牙文",
    "es_mx": "西班牙文",
    "es_ar": "西班牙文",
    "ru_ru": "俄文",
    "it_it": "意大利文",
    "pt_br": "葡萄牙文",
    "pt_pt": "葡萄牙文",
    "vi_vn": "越南文",
    "th_th": "泰文",
    "id_id": "印度尼西亚文",
    "uk_ua": "乌克兰文",
    "pl_pl": "波兰文",
    "tr_tr": "土耳其文",
    "nl_nl": "荷兰文",
}


def lang_display_name(target_lang: str) -> str:
    """target_lang → 显示名：zh_cn→简体中文、zh_tw→繁体中文、日文→日文……未知代码原样。"""
    return _LANG_DISPLAY.get(target_lang, target_lang)


def friendly_output_name(jar: Path, target_lang: str = "zh_cn") -> str:
    """汉化 jar 友好产物名：{中文名}-{语言}化.jar（zh_cn→简体中文化、ja_jp→日文化）。

    中文名取 resolve_mod_name（含 CJK 且 ≥2 字）；取不到中文名回退原 jar stem。
    后缀「{语言}化」严格跟随 target_lang——用户选中什么语言就命名为什么语言。
    """
    name = resolve_mod_name(jar)
    base = name if usable_chinese_name(name) else jar.stem
    # 修复（recheck）：displayName 可能含 Windows 非法字符（< > : " / \ | ? * 控制字符）——
    # 不清理则 shutil.copy2 抛 OSError（WinError 123）使任务失败；统一替换为下划线并去首尾空白/点
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base).strip().strip(".")
    if not base:
        base = jar.stem
    return f"{base}-{lang_display_name(target_lang)}化.jar"
