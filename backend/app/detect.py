# 任务 A1：输入类型 / 源语言 / pack_format 自动识别
# 让用户「拖进来/选路径」后后端自动判断：是整合包还是 mod jar 还是地图、源语言是什么、资源包格式版本多少。
# 翻译流程用 needs_translation 替代直接调 should_translate（先判断是否已汉化）。
import json
import re
import zipfile
from collections import Counter
from pathlib import Path

from app.jar import list_jar_lang_files
from app.langfile import parse_json_lang, parse_lang
from app.maps.world import validate_world
from app.translate.common import should_translate

# detect 阶段硬编码扫描的 jar 大小上限：超过跳过深扫记 None（轻量识别，深扫留给 A5 流式）。
# 200MB：扫描硬编码要解压 jar + 逐个 class 解析字节码，超大 jar 慢/占资源；但大 mod
# （Enders Cataclysm 等 50-150MB）的硬编码文本同样重要，跳过会漏翻（用户反馈）——
# 上限从 50MB 提到 200MB，覆盖绝大多数 mod，仅极巨型 jar 才跳过并明确提示。
_HARDCODE_MAX_BYTES = 200 * 1024 * 1024   # 200MB（auto_flow 硬编码候选收集用：超大 jar 跳过深扫）

# 已汉化判定：目标语言为简体/繁体中文，按「中文字符占比 > 40%」判断
# （纯中文 → 已汉化跳过；"Click here 点击" 这类英文为主混排 → 仍需翻译）
_CJK_RE = re.compile(r"[一-鿿㐀-䶿]")
_ZH_RATIO = 0.4


def _is_project_root(p: Path) -> bool:
    """是否整合包/地图/光影的项目根（含直接标志）：
    mods/（整合包）、level.dat / region/ / entities/（地图）、shaders/lang/（光影）。"""
    return ((p / "mods").is_dir()
            or (p / "level.dat").is_file()
            or (p / "region").is_dir()
            or (p / "entities").is_dir()
            or (p / "shaders" / "lang").is_dir())


def unwrap_bare_wrapper(path: Path) -> Path:
    """解压目录被单一顶层目录包裹（zip 里 `xxxx/主文件夹` 嵌套结构）→ 下钻到项目根。

    整合包/地图/光影 zip 常见打包形态：解压后是 `xxx/` 一层包裹，`mods/`、`level.dat`、
    `shaders/lang` 都在包裹层内——只看解压根目录会漏判成 unknown（用户实测）。规则：
    当前目录不含任何项目标志、且恰好只有一个非隐藏子目录（无项目文件）→ 判定为包裹层
    下钻；循环直到找到项目根或无法下钻。__MACOSX/.DS_Store 等打包垃圾忽略。
    """
    p = Path(path)
    if not p.is_dir():
        return p    # 文件输入（单 jar 等）不下钻
    seen: set[Path] = set()   # 修复（recheck）：防 symlink/junction 成环无限下钻
    while True:
        if _is_project_root(p):
            return p
        if p in seen:
            return p
        seen.add(p)
        try:
            subdirs = [d for d in p.iterdir() if d.is_dir()
                       and not d.name.startswith((".", "__"))]
            files = [f for f in p.iterdir() if f.is_file() and f.name != ".DS_Store"]
        except OSError:
            return p    # 修复（recheck）：无权限/正被删除的目录 → 不下钻，按当前目录识别
        if len(subdirs) == 1 and not files:
            if len(seen) >= 16:
                return p    # 深度上限（防御性：正常包裹层 ≤2 层，恶意深层包裹不无限下钻）
            p = subdirs[0]
            continue
        return p


def detect_input_type(path: Path) -> str:
    """识别输入类型：modpack | modjar | map | shader | unknown。

    轻量原则：.jar/.mcworld 只按后缀判断；压缩包（.zip/.mrpack）不解压，
    交给 /api/detect 端点先 _resolve 解压成目录后再按目录规则判断，
    因此本函数对未解压的压缩包返回 unknown（.mcworld 后缀即地图产物，例外）。
    shader：光影包目录含 shaders/lang/ 语言文件（光影汉化的通用标志，不特判任何光影）。
    目录：先下钻包裹层（zip 解压后的 xxxx/ 嵌套），再按项目标志判断类型。
    """
    p = Path(path)
    if p.is_dir():
        p = unwrap_bare_wrapper(p)
        # 目录：含可加载 level.dat → 地图；含 mods/ → 整合包；含 shaders/lang → 光影包。
        # 兼容维度包/不完整存档（用户实测 DIM-1 维度 zip 无 level.dat 仍应识别为地图）：
        # 含 region/ 或 entities/（世界区块）也判 map
        if validate_world(p):
            return "map"
        if (p / "region").is_dir() or (p / "entities").is_dir():
            return "map"
        if (p / "mods").is_dir():
            return "modpack"
        # 修复（recheck）：纯 Modrinth .mrpack 包内无 mods/（mods 由 modrinth.index.json
        # 的 files 数组网络下载，zip 内只有 index + overrides/）——之前判 unknown 无法翻译。
        # 补 CurseForge（manifest.json）/ packwiz（pack.toml）整合包特征：三者都是整合包
        # 根目录标志，优先级高于 shader（纯资源包内置光影不误判为光影包）
        if (p / "modrinth.index.json").is_file() or (p / "manifest.json").is_file() \
                or (p / "pack.toml").is_file():
            return "modpack"
        if (p / "shaders" / "lang").is_dir():
            return "shader"
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
    """目标为 zh_cn/zh_tw 且中文字符占比 > 40% → 已汉化，跳过翻译；
    否则复用 should_translate（注意它保留 "Hello World" 这类纯 ASCII 空格串）。

    占比而非「含任意 CJK」：混排文本（"Click here 点击" 英文为主）若只因
    夹带少量中文就被判已汉化会漏翻——英文主混排仍需翻译。
    """
    if target_lang in ("zh_cn", "zh_tw") and text:
        zh = len(_CJK_RE.findall(text))
        if zh / max(len(text), 1) > _ZH_RATIO:
            return False
    return should_translate(text)


def needs_lang_value_translation(text: str, target_lang: str) -> bool:
    """语言文件值翻译判定：**含目标语言字符即视为已汉化**（跳过），否则需翻译。

    修复（recheck）：原「中文占比 >40% 判已汉化」是拍脑袋阈值，无开源依据——成熟做法是
    mods-string-extractor 的 en_us−zh_cn **key 差集**（有 key 即算已翻译）+ Aaalice 的
    词典参考命中。我们保留一层值语言校验（防 Sodium 场景：zh_cn 存在但值是英文占位 →
    key 差集会误判已翻译 → 英文残留）：**含任一目标语言字符即已汉化**（对齐翻译侧
    _is_target_lang 判定）——「钻石 Diamond」这类部分翻译（专有名词保留英文）是正常汉化
    实践，跳过不重翻；纯英文值（假翻译占位）补翻。

    语言文件值本就是可翻译文本（键才是标识符），"Requires_Armor" 这类 snake_case 真实
    短语必须放行；长度/纯数字已在扫描层由 lang_value_ok 宽松过滤。结构化 JSON / 硬编码
    等技术串过滤仍走 needs_translation（should_translate）。

    例外：带点无空格的类路径/域名/带点标识符（com.example.Mod / path.to.x）不是显示文本
    （用户判定「中间带点无空格不是句子」）→ 跳过不翻译。
    """
    if target_lang in ("zh_cn", "zh_tw") and text:
        if _CJK_RE.search(text):
            return False    # 已含汉字 → 已汉化，跳过
    elif target_lang == "ja_jp" and text:
        if re.search(r"[぀-ヿ]", text):
            return False    # 已含假名 → 已汉化
    elif target_lang == "ko_kr" and text:
        if re.search(r"[가-힯]", text):
            return False    # 已含谚文 → 已汉化
    if ("." in text and re.search(r"\.[a-zA-Z0-9_]", text)
            and not re.search(r"\.\s", text)):
        return False    # 带点无空格类路径/标识符 → 非文本，跳过
    return True


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


def _manifest_mc_version(pack: Path) -> str:
    """整合包根目录显式版本文件 → MC 版本（优先级最高，最准；找不到返回空串）。

    覆盖常见整合包格式：pack.toml（Modrinth .mrpack）/ manifest.json（CurseForge /
    Modrinth）/ mmc-pack.json（MultiMC / Prism）/ instance.json（ATLauncher）/
    version.json（根字段 id/name）。解析失败/字段缺失静默返回空（走 mods 元数据兜底）。
    """
    try:
        pt = pack / "pack.toml"
        if pt.exists():
            m = re.search(r"minecraft\s*=\s*[\"']([\d.]+)[\"']", pt.read_text(encoding="utf-8"))
            if m:
                return m.group(1)
        mf = pack / "manifest.json"
        if mf.exists():
            d = json.loads(mf.read_text(encoding="utf-8"))
            mc = d.get("minecraft") if isinstance(d, dict) else None
            v = mc.get("version") if isinstance(mc, dict) else (mc if isinstance(mc, str) else None)
            if v:
                return str(v)
        mmc = pack / "mmc-pack.json"
        if mmc.exists():
            d = json.loads(mmc.read_text(encoding="utf-8"))
            for c in d.get("components", []) if isinstance(d, dict) else []:
                if c.get("uid") == "minecraft" and c.get("version"):
                    return str(c["version"])
        inst = pack / "instance.json"
        if inst.exists():
            d = json.loads(inst.read_text(encoding="utf-8"))
            launcher = d.get("launcher") if isinstance(d, dict) else None
            v = launcher.get("minecraftVersion") if isinstance(launcher, dict) else None
            if not v:
                v = d.get("minecraftVersion") if isinstance(d, dict) else None
            if v:
                return str(v)
        vj = pack / "version.json"
        if vj.exists():
            d = json.loads(vj.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                for key in ("id", "name"):
                    v = d.get(key)
                    if v and re.search(r"\d+\.\d+", str(v)):
                        return str(v)
    except Exception:
        pass
    return ""


def _jar_mc_version_direct(jar: Path) -> str:
    """读单个 jar **自身**元数据的 MC 版本：fabric.mod.json depends.minecraft / mods.toml
    或 neoforge.mods.toml 的 minecraft versionRange。损坏 jar / 无元数据返回空串。
    复用 vp 的版本提取与 toml 解析，避免重复实现。"""
    from app.vp import _extract_mc_version, _toml_mc_spec
    try:
        with zipfile.ZipFile(jar) as zf:
            names = zf.namelist()
            if "fabric.mod.json" in names:
                d = json.loads(zf.read("fabric.mod.json").decode("utf-8"))
                spec = str((d.get("depends") or {}).get("minecraft", ""))
                v = _extract_mc_version(spec)
                if v:
                    return v
            for toml in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
                if toml in names:
                    v = _extract_mc_version(_toml_mc_spec(zf, toml))
                    if v:
                        return v
    except Exception:
        pass
    return ""


def detect_mc_version(pack: Path) -> str:
    """检测整合包 / mod jar 的 MC 版本（多来源自动识别，返回空串表示无法确定）。

    优先级：
      1) 整合包根显式版本文件（pack.toml / manifest.json / mmc-pack.json / instance.json /
         version.json——最准，直接是整合包声明的 MC 版本）；
      2) mods/*.jar 元数据（fabric.mod.json 的 depends.minecraft、mods.toml /
         neoforge.mods.toml 的 minecraft versionRange，infer_modpack_runtime 遍历取第一个命中）；
      3) 单 mod jar 直接查自身元数据。

    供 build 阶段把资源包 pack_format 按真实 MC 版本注入（材质包兼容，不靠猜）；
    识别不到时调用方回退语言文件后缀 / 已存在 pack.mcmeta 推断。
    """
    from app.vp import infer_modpack_runtime
    p = Path(pack)
    if p.is_file():
        # 修复（recheck）：读该 jar **自身**元数据——之前扫父目录会取到遍历顺序第一个 jar
        # 的版本（从 mods 文件夹选单个 jar 会拿错版本 → pack_format 错 → 资源包被游戏拒载）
        return _jar_mc_version_direct(p)
    v = _manifest_mc_version(p)
    if v:
        return v
    _l, _v = infer_modpack_runtime(p / "mods")
    return _v


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
                raw = zf.read(info["path"]).decode("utf-8-sig")   # 剥 BOM（带 BOM 词条估算失真）
                entries = parse_json_lang(raw) if info["format"] == "json" else parse_lang(raw)
                total += len(entries)
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return 0
    return total


def build_detect_summary(jars: list[Path], source_lang: str | None) -> dict:
    """统计识别结果摘要：各 jar 语言文件数 + 词条数估算。

    **不扫硬编码候选**（修复：整合包几百个 jar 逐个解析 class 字节码扫硬编码极慢，
    用户实测「卡在识别中」）。硬编码扫描留给翻译流程（A5 流式处理）。
    total_hardcoded/hardcoded_candidates 返回 None，前端据此隐藏硬编码候选展示。
    """
    total_lang = 0
    total_entries = 0
    per: list[dict] = []
    for jar in jars:
        try:
            infos = list_jar_lang_files(jar)
        except (zipfile.BadZipFile, OSError):
            continue
        total_lang += len(infos)
        entries = _estimate_entries(jar, infos, source_lang)
        total_entries += entries
        per.append({
            "jar": jar.name,
            "lang_files": len(infos),
            "entries": entries,
        })
    return {
        "jar_count": len(per),
        "total_lang_files": total_lang,
        "total_entries": total_entries,
        "total_hardcoded": None,
        "hardcoded_candidates": None,
        "jars": per,
    }
