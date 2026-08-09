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


def load_scan_keys(path: Path) -> set[str]:
    """读键白名单（json 数组）。"""
    return set(json.loads(path.read_text(encoding="utf-8")))


def is_translatable_text(text: str) -> bool:
    """复用引擎技术串过滤：路径/标识符/纯数字/UUID 跳过。"""
    return should_translate(text)


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


def scan_nbt(data, keys: set[str], acc: list, path_str: str = "", depth: int = 0) -> None:
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
                if str(k) in keys and is_translatable_text(val):
                    acc.append({"text": val, "nbt_path": child, "key": str(k)})
            else:
                scan_nbt(v, keys, acc, child, depth + 1)
    elif kind == "list":
        for i, item in enumerate(items):
            val = _string_value(item)
            if val is not None:
                if is_translatable_text(val):
                    acc.append({"text": val, "nbt_path": f"{path_str}[{i}]", "key": "list"})
            else:
                scan_nbt(item, keys, acc, f"{path_str}[{i}]", depth + 1)


def scan_dat(path: Path, keys: set[str]) -> list[dict]:
    """NBT 文件扫描，损坏文件跳过。"""
    acc: list[dict] = []
    try:
        scan_nbt(File.load(path, gzipped=True), keys, acc)
    except Exception:
        pass
    return acc


def scan_json_text(path: Path, keys: set[str]) -> list[dict]:
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
                if isinstance(v, str) and is_translatable_text(v):
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


def scan_mcfunction(path: Path, keys: set[str]) -> list[dict]:
    """.mcfunction 扫描：say 取引号文本，tellraw/title 参数若是 JSON 则递归取其 text。"""
    acc: list[dict] = []

    def append_text(txt: str, i: int) -> None:
        if is_translatable_text(txt):
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


def scan_mca(path: Path, keys: set[str]) -> list[dict]:
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
            for key in ("TileEntities", "block_entities"):
                if key in level:
                    scan_nbt(level[key], keys, acc, f"chunk({cx},{cz})")
    return acc


def scan_file(file: Path, keys: set[str]) -> list[dict]:
    """按后缀分派扫描。.dat/.mca/.json/.mcfunction 各有专属逻辑。"""
    suffix = file.suffix.lower()
    if suffix == ".dat":
        return scan_dat(file, keys)
    if suffix == ".mca":
        return scan_mca(file, keys)
    if suffix == ".json":
        return scan_json_text(file, keys)
    if suffix == ".mcfunction":
        return scan_mcfunction(file, keys)
    return []


def scan_world(world: Path) -> list[dict]:
    """扫描整档副本。"""
    keys = load_scan_keys(Path(__file__).parent / "scan_keys.json")
    acc: list[dict] = []
    for f in list_world_files(world):
        acc.extend(scan_file(f, keys))
    return acc
