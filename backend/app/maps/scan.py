"""M4-3 地图 NBT 扫描模块。

扫描世界存档副本里的可翻译文本：NBT 文件（level.dat/playerdata 的命令方块、
告示牌、书与笔等）、数据包 JSON（进度/成就）、.mcfunction（say/tellraw/title）、
以及 .mca 区块里的 block entity（命令方块）文本。
全程 pathlib 路径，只读副本，不碰原档。
"""
import json
import re
from pathlib import Path

from nbtlib import Compound, File, List, String

from app.maps.world import list_world_files
from app.translate.common import should_translate


# 扫描键白名单默认值（与 scan_keys.json 一致）：scan_keys.json 缺失/损坏/打包只读时兜底，
# 避免 frozen 环境缺文件崩启动（用户实测 Permission denied 写 _MEIPASS\app\maps）。
# text 组件字段：告示牌 front_text.messages[i].text、自定义文本、书页内组件——剧情地图
# 大量告示牌/剧情文本走 text 组件，之前漏白名单导致扫描只出数据值、剧情文本全丢（用户实测）。
_DEFAULT_SCAN_KEYS = ("Command", "CustomName", "Name", "front_text", "Text1",
                      "Text2", "Text3", "Text4", "pages", "title", "author", "text")


def load_scan_keys(path: Path) -> set[str]:
    """读键白名单（json 数组）。读取失败 → 内置默认键兜底，绝不崩。"""
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return set(_DEFAULT_SCAN_KEYS)


def is_translatable_text(text: str, target_lang: str | None = None) -> bool:
    """地图 NBT 文本过滤（参考 maptranslator：标记可翻译节点，排除动态值/变量）。

    排除游戏动态值/占位符（不是显示文本，翻译会破坏指令/变量替换）：
      - $selected / $(var) 变量替换
      - @p / @a selector
      - %s / %d 格式占位符
      - #score 记分板目标
    其余复用 should_translate（路径/标识符/纯数字跳过）。
    修复：断点重连副本已写回中文 → 纯中文（无字母）直接跳过，不重复收集/翻译；
    目标语言是中文系（zh_cn/zh_tw）时**不跳过**——繁体/方言作者的中文源文本仍需简繁转换，
    否则 scan 层误跳导致简繁转换漏翻（回归修复）。
    """
    t = text.strip()
    if not t or t.startswith(("$", "@", "%", "#")) or "§" in t:
        return False
    if re.search(r"[一-鿿]", t) and not re.search(r"[a-zA-Z]", t):
        if target_lang not in ("zh_cn", "zh_tw"):
            return False    # 纯中文：已汉化（断点重连不重复）；目标中文系则需简繁转换
    return should_translate(t)


def _string_value(v) -> str | None:
    """提取字符串值，兼容 nbtlib 的 String 与 anvil 用的 nbt 库 TAG_String。"""
    if isinstance(v, str):  # nbtlib String 是 str 子类
        return v
    val = getattr(v, "value", None)
    return val if isinstance(val, str) else None


def _children(data):
    """识别节点类型并返回 (kind, items)，兼容 nbtlib 与 nbt 库对象。

    kind 为 "compound"（items 为 (键, 值) 对）或 "list"（items 为元素列表）。
    """
    if isinstance(data, Compound):
        return "compound", list(data.items())
    if isinstance(data, List):
        return "list", list(data)
    tags = getattr(data, "tags", None)
    if isinstance(tags, list):
        # nbt 库：TAG_Compound 的元素带名字，TAG_List 的元素不带
        if tags and all(getattr(t, "name", None) for t in tags):
            return "compound", [(t.name, t) for t in tags]
        return "list", list(tags)
    if isinstance(data, dict):
        return "compound", list(data.items())
    if isinstance(data, list):
        return "list", list(data)
    return None, []


def scan_nbt(data, keys: set[str], acc: list, path_str: str = "", depth: int = 0,
             target_lang: str | None = None) -> None:
    """递归 Compound/List，String 值命中白名单键且可翻则收集。

    List 中的 String 元素（如书与笔的 pages）无键名，直接以 key="list" 收集；
    Compound 值若非 String 则递归（如 front_text 的 messages 列表）。
    """
    if depth > 64:
        return
    kind, items = _children(data)
    if kind == "compound":
        for k, v in items:
            child = f"{path_str}.{k}"
            val = _string_value(v)
            if val is not None:
                if str(k) == "Command":
                    # 命令方块 Command：只收指令的文本参数（say 引号文本 / JSON text），
                    # 保留指令字——整体翻译会把指令翻成中文导致无法执行（用户实测）
                    _scan_command_text(val, child, acc, target_lang)
                elif str(k) in keys:
                    # JSON 组件（CustomName = {"text":"NPC","color":"green"}）→ 只翻 text 值，
                    # 写回时在组件内 replace 保留 JSON 结构（整体翻译会破坏组件，对齐
                    # WorldTranslationExtractor 的 text 组件提取思路）
                    try:
                        obj = json.loads(val)
                        if isinstance(obj, (dict, list)):
                            has_text = False
                            for t in _iter_json_texts(obj):
                                if is_translatable_text(t, target_lang):
                                    acc.append({"text": t, "nbt_path": child, "key": str(k)})
                                    has_text = True
                            if has_text:
                                continue
                    except Exception:
                        pass
                    if is_translatable_text(val, target_lang):
                        acc.append({"text": val, "nbt_path": child, "key": str(k)})
            else:
                scan_nbt(v, keys, acc, child, depth + 1, target_lang)
    elif kind == "list":
        for i, item in enumerate(items):
            val = _string_value(item)
            if val is not None:
                if is_translatable_text(val, target_lang):
                    acc.append({"text": val, "nbt_path": f"{path_str}[{i}]", "key": "list"})
            else:
                scan_nbt(item, keys, acc, f"{path_str}[{i}]", depth + 1, target_lang)


def scan_dat(path: Path, keys: set[str], target_lang: str | None = None) -> list[dict]:
    """NBT 文件扫描，损坏文件跳过。"""
    acc: list[dict] = []
    try:
        scan_nbt(File.load(path, gzipped=True), keys, acc, target_lang=target_lang)
    except Exception:
        pass
    return acc


def scan_json_text(path: Path, keys: set[str], target_lang: str | None = None) -> list[dict]:
    """数据包 JSON（进度/成就）扫描，递归找可翻译字符串。"""
    acc: list[dict] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return acc

    def walk(o, prefix: str, depth: int = 0) -> None:
        if depth > 64:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                child = k if not prefix else f"{prefix}.{k}"
                if isinstance(v, str) and is_translatable_text(v, target_lang):
                    acc.append({"text": v, "nbt_path": child, "key": str(k)})
                else:
                    walk(v, child, depth + 1)
        elif isinstance(o, list):
            for i, item in enumerate(o):
                walk(item, f"{prefix}[{i}]", depth + 1)

    walk(data, "")
    return acc


def _iter_json_texts(obj):
    """递归取 JSON 文本组件里的 text / extra[].text 字符串。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "text" and isinstance(v, str):
                yield v
            elif k == "extra" and isinstance(v, list):
                for e in v:
                    yield from _iter_json_texts(e)
            elif isinstance(v, (dict, list)):
                yield from _iter_json_texts(v)
    elif isinstance(obj, list):
        for e in obj:
            yield from _iter_json_texts(e)


def scan_mcfunction(path: Path, keys: set[str], target_lang: str | None = None) -> list[dict]:
    """.mcfunction 扫描：say 取引号文本，tellraw/title 参数若是 JSON 则递归取其 text。"""
    acc: list[dict] = []

    def append_text(txt: str, i: int) -> None:
        if is_translatable_text(txt, target_lang):
            acc.append({"text": txt, "nbt_path": f"line{i}", "key": "mcfunction"})

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return acc
    for i, line in enumerate(lines):
        for m in re.finditer(r"(say|tellraw\s+\S+|title\s+\S+\s+\S+)\s+(.+)", line):
            cmd, rest = m.group(1), m.group(2).strip()
            if cmd.startswith("say"):
                append_text(rest.strip('"'), i)
                continue
            # tellraw/title：参数若为 JSON（{...}），递归取其 text/extra[].text
            try:
                obj = json.loads(rest)
            except Exception:
                append_text(rest.strip('"'), i)
                continue
            for t in _iter_json_texts(obj):
                append_text(t, i)
    return acc


def _scan_command_text(cmd: str, nbt_path: str, acc: list, target_lang: str | None = None) -> None:
    """命令方块 Command：只收集指令的文本参数（say 引号文本 / tellraw·title·actionbar
    ·bossbar 的 JSON text），保留指令字与结构——整体翻译会把指令翻成中文无法执行
    （用户实测）。对应 WorldTranslationExtractor 的 command_block 提取思路。
    """
    for m in re.finditer(
            r"(?:^|;)\s*(say|tellraw\s+\S+|title\s+\S+\s+\S+|actionbar\s+\S+|bossbar\s+add\s+\S+)\s+(.+)",
            cmd, re.IGNORECASE):
        head, rest = m.group(1), m.group(2).strip()
        if head.lower().startswith("say"):
            t = rest.strip('"')
            if is_translatable_text(t, target_lang):
                acc.append({"text": t, "nbt_path": nbt_path, "key": "Command"})
            continue
        # tellraw/title/actionbar/bossbar：参数若为 JSON 组件，递归取其 text
        try:
            obj = json.loads(rest)
        except Exception:
            continue
        for t in _iter_json_texts(obj):
            if is_translatable_text(t, target_lang):
                acc.append({"text": t, "nbt_path": nbt_path, "key": "Command"})


def scan_mca(path: Path, keys: set[str], target_lang: str | None = None) -> list[dict]:
    """扫描 .mca region 区块里的 block entity（命令方块）文本。

    依赖 anvil-parser（内部用 nbt 库）。遍历 32×32 区块，对每个区块 NBT 中
    TileEntities（旧）与 block_entities（新）列表走 scan_nbt。
    区块缺失/损坏或库不可用时跳过该区块。
    """
    acc: list[dict] = []
    try:
        import anvil
    except Exception:
        return acc
    try:
        region = anvil.Region.from_file(str(path))
    except Exception:
        return acc
    for cz in range(32):
        for cx in range(32):
            try:
                root = region.chunk_data(cx, cz)
            except Exception:
                continue
            if root is None:
                continue
            # 兼容新/旧 chunk 布局：Level 包裹（≤1.14）或平铺（1.15+）
            level = root["Level"] if "Level" in root else root
            # block_entities（告示牌/命令方块/讲台）+ entities（实体 CustomName/Name，
            # 剧情地图 NPC 名字/展示框/盔甲架——之前漏扫导致剧情地图文本偏少）
            # 修复：路径前缀携带列表名（chunk(x,z).block_entities[i] / .entities[i]），
            # 写回侧据此按名定位——否则 block_entities 与 entities 并存时下标错位，
            # 实体译文被写进 block_entities 或静默丢失
            for key in ("TileEntities", "block_entities", "entities"):
                if key in level:
                    scan_nbt(level[key], keys, acc, f"chunk({cx},{cz}).{key}", target_lang=target_lang)
    return acc


def scan_file(file: Path, keys: set[str], target_lang: str | None = None) -> list[dict]:
    """按后缀分派扫描。.dat/.mca/.json/.mcfunction 各有专属逻辑。"""
    suffix = file.suffix.lower()
    if suffix == ".dat":
        return scan_dat(file, keys, target_lang)
    if suffix == ".mca":
        return scan_mca(file, keys, target_lang)
    if suffix == ".json":
        return scan_json_text(file, keys, target_lang)
    if suffix == ".mcfunction":
        return scan_mcfunction(file, keys, target_lang)
    return []


def scan_world(world: Path, target_lang: str | None = None) -> list[dict]:
    """扫描整档副本。给每个 entry 补 file 字段（写回需要知道目标文件）。"""
    keys = load_scan_keys(Path(__file__).parent / "scan_keys.json")
    acc: list[dict] = []
    for f in list_world_files(world):
        for e in scan_file(f, keys, target_lang):
            acc.append({**e, "file": str(f)})
    return acc
