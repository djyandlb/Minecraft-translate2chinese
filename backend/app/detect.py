# 任务 A1：输入类型 / 源语言 / pack_format 自动识别
# 让用户「拖进来/选路径」后后端自动判断：是整合包还是 mod jar 还是地图、源语言是什么、资源包格式版本多少。
# 翻译流程用 needs_translation 替代直接调 should_translate（先判断是否已汉化）。
import json
import re
import zipfile
from collections import Counter
from pathlib import Path

from app.hardcode import scan_hardcoded_strings
from app.jar import list_jar_lang_files
from app.langfile import parse_json_lang, parse_lang
from app.maps.world import validate_world
from app.translate.common import should_translate

# detect 阶段硬编码扫描的 jar 大小上限：超过跳过深扫记 None（轻量识别，深扫留给 A5 流式）
_HARDCODE_MAX_BYTES = 50 * 1024 * 1024  # 50MB

# 已汉化判定：目标语言为简体/繁体中文且文本含 CJK 统一表意文字块
_CJK_RE = re.compile(r"[一-鿿㐀-䶿]")


def detect_input_type(path: Path) -> str:
    """识别输入类型：modpack | modjar | map | unknown。

    轻量原则：.jar/.mcworld 只按后缀判断；压缩包（.zip/.mrpack）不解压，
    交给 /api/detect 端点先 _resolve 解压成目录后再按目录规则判断，
    因此本函数对未解压的压缩包返回 unknown（.mcworld 后缀即地图产物，例外）。
    """
    p = Path(path)
    if p.is_dir():
        # 目录：含可加载 level.dat → 地图；含 mods/ → 整合包
        if validate_world(p):
            return "map"
        if (p / "mods").is_dir():
            return "modpack"
        return "unknown"
    suffix = p.suffix.lower()
    if suffix == ".jar":
        return "modjar"
    if suffix == ".mcworld":
        return "map"
    return "unknown"


def detect_source_lang(jars: list[Path], target_lang: str) -> str | None:
    """聚合所有 jar 的语言文件 lang 名统计出现次数，排除 target_lang。

    取出现次数最多者；同频下优先 en_* 系；再取字典序最小保证确定性。
    全部已汉化（排除 target_lang 后无其他语言）→ 返回 None。
    """
    counts: Counter[str] = Counter()
    for jar in jars:
        try:
            for info in list_jar_lang_files(jar):
                if info["lang"] != target_lang:
                    counts[info["lang"]] += 1
        except (zipfile.BadZipFile, OSError):
            # 损坏/不可读 jar：跳过该 jar，不让一个坏文件影响整体识别
            continue
    if not counts:
        return None
    top = max(counts.values())
    # 同频优先 en_*（如 en_us/en_gb 混装时英文系优先）
    en_cands = sorted(lang for lang, c in counts.items() if c == top and lang.startswith("en_"))
    if en_cands:
        return en_cands[0]
    return min(lang for lang, c in counts.items() if c == top)


def needs_translation(text: str, target_lang: str) -> bool:
    """目标为 zh_cn/zh_tw 且文本已含 CJK → 已汉化，跳过翻译；
    否则复用 should_translate（注意它保留 "Hello World" 这类纯 ASCII 空格串）。"""
    if target_lang in ("zh_cn", "zh_tw") and _CJK_RE.search(text):
        return False
    return should_translate(text)


def _read_pack_format_bytes(raw: bytes) -> int | None:
    """从 pack.mcmeta 字节读 pack_format；缺失/损坏返回 None（A1-review：畸形 pack 字段不 500）。"""
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            return None
        pack = data.get("pack")
        if not isinstance(pack, dict):
            return None
        return int(pack.get("pack_format"))
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _dir_pack_format(d: Path) -> int | None:
    """读目录根 pack.mcmeta 的 pack_format；无/不可读返回 None。"""
    pm = d / "pack.mcmeta"
    if pm.is_file():
        try:
            return _read_pack_format_bytes(pm.read_bytes())
        except OSError:
            return None
    return None


def _jar_pack_format(jar: Path) -> int | None:
    """查 jar 内根 pack.mcmeta 的 pack_format；无/损坏返回 None。"""
    try:
        with zipfile.ZipFile(jar) as zf:
            if "pack.mcmeta" in zf.namelist():
                return _read_pack_format_bytes(zf.read("pack.mcmeta"))
    except (zipfile.BadZipFile, OSError):
        return None
    return None


def _pack_format_from_lang_suffix(lang_infos: list[dict]) -> int:
    """按语言文件后缀推断：任一 .lang → 3（1.12- 用 .lang）；否则 .json → 15（1.20.1）。"""
    for info in lang_infos:
        if info["format"] == "lang":
            return 3
    return 15


def infer_pack_format(path: Path) -> int:
    """推断资源包格式版本（pack_format）。

    来源优先级：
      1) pack.mcmeta 的 pack.pack_format（目录浅找：根 → mods/*.jar 内第一个命中；jar 直接查内）
      2) 语言文件后缀：任一 .lang → 3；.json → 15
      3) 默认 15
    """
    p = Path(path)
    if p.is_file():
        fmt = _jar_pack_format(p)
        if fmt is not None:
            return fmt
        try:
            return _pack_format_from_lang_suffix(list_jar_lang_files(p))
        except (zipfile.BadZipFile, OSError):
            return 15
    # 目录：根 pack.mcmeta
    fmt = _dir_pack_format(p)
    if fmt is not None:
        return fmt
    # 整合包 mods/*.jar 内扫描（第一个命中 pack.mcmeta）
    mods = p / "mods"
    jars = sorted(mods.rglob("*.jar")) if mods.is_dir() else []
    for jar in jars:
        fmt = _jar_pack_format(jar)
        if fmt is not None:
            return fmt
    # 语言文件后缀兜底
    for jar in jars:
        try:
            infos = list_jar_lang_files(jar)
            if infos:
                return _pack_format_from_lang_suffix(infos)
        except (zipfile.BadZipFile, OSError):
            continue
    return 15


def _estimate_entries(jar: Path, infos: list[dict], source_lang: str | None) -> int:
    """词条数估算：优先统计 source_lang 的语言文件词条；source_lang 为 None（全汉化）时对所有语言求和。"""
    total = 0
    try:
        with zipfile.ZipFile(jar) as zf:
            for info in infos:
                if source_lang is not None and info["lang"] != source_lang:
                    continue
                raw = zf.read(info["path"]).decode("utf-8")
                entries = parse_json_lang(raw) if info["format"] == "json" else parse_lang(raw)
                total += len(entries)
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return 0
    return total


def _scan_hardcoded(jar: Path) -> int | None:
    """扫描单个 jar 的硬编码可翻译字符串数；异常或超过大小上限返回 None（未统计）。"""
    try:
        if jar.stat().st_size > _HARDCODE_MAX_BYTES:
            return None
        return len(scan_hardcoded_strings(jar))
    except Exception:
        return None


def build_detect_summary(jars: list[Path], source_lang: str | None) -> dict:
    """统计识别结果摘要：各 jar 语言文件数 + 词条数估算 + 硬编码字符串数。

    硬编码策略：对每个 jar 调 scan_hardcoded_strings；异常兜底记 None；
    超过 50MB 的 jar 跳过深扫记 None（detect 阶段保持轻量，深扫留给 A5 流式处理）。
    """
    total_lang = 0
    total_entries = 0
    total_hard = 0
    hard_unknown = 0
    per: list[dict] = []
    for jar in jars:
        try:
            infos = list_jar_lang_files(jar)
        except (zipfile.BadZipFile, OSError):
            continue
        total_lang += len(infos)
        entries = _estimate_entries(jar, infos, source_lang)
        total_entries += entries
        hard = _scan_hardcoded(jar)
        if hard is None:
            hard_unknown += 1
        else:
            total_hard += hard
        per.append({
            "jar": jar.name,
            "lang_files": len(infos),
            "entries": entries,
            "hardcoded": hard,
        })
    return {
        "jar_count": len(per),
        "total_lang_files": total_lang,
        "total_entries": total_entries,
        "total_hardcoded": total_hard if hard_unknown == 0 else None,
        "jars": per,
    }
