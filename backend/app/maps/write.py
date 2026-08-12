"""M4-4 地图副本写回模块。

消费 scan.py 产出的 {text, nbt_path, key, file} 结构，把译文写回世界副本的
NBT / JSON / mcfunction / mca 文件。写前把原文件备份为 <name>.bak（若不存在）。
全程 pathlib 路径，只写副本，不碰原档。
"""
import gzip
import io
import json
import re
import shutil
import struct
import zlib
from pathlib import Path

from nbtlib import Compound, File, List, String


def _split_path(nbt_path: str) -> list:
    """'a.b[0].c' → ['a', 0, 'c']；'[0].c' → [0, 'c']；[i] 转 int 下标。

    支持 [i] 开头的列表路径（.mca 的 block_entities/TileEntities 元素下标）。
    键名允许含 : / - 等（修复：minecraft:story/root.title 这类含特殊字符的键名段
    原被字符类拒绝而丢弃，导致 JSON 写回错位或静默丢失）。
    """
    parts: list = []
    for seg in nbt_path.split("."):
        m = re.fullmatch(r"(?:([^\[\]]+?)(?:\[(\d+)\])?|\[(\d+)\])", seg)
        if not m:
            continue
        if m.group(1) is not None:
            parts.append(m.group(1))
        idx = m.group(2) if m.group(2) is not None else m.group(3)
        if idx is not None:
            parts.append(int(idx))
    return parts


def _set_value(data, parts: list, translated: str, original: str | None = None) -> None:
    """沿路径下钻到最后一段，赋新的 String 值。

    original 传入（命令方块 Command 文本参数）：只替换值里第一次出现的原文文本，
    保留指令字/结构；否则整值替换。
    """
    node = data
    for p in parts[:-1]:
        node = node[p]
    last = parts[-1]
    if (original is not None and last in node
            and isinstance(node[last], String) and original in str(node[last])):
        node[last] = String(str(node[last]).replace(original, translated, 1))
    else:
        # nbtlib 的 Compound 键与 List 下标均支持赋值；List 下标赋 String 自动入列
        node[last] = String(translated)


def apply_translation(nbt_data, nbt_path: str, translated: str,
                      original: str | None = None) -> None:
    """按路径替换 NBT String 值。路径形如 Data.Command、Data.pages[0]。

    若路径以根段 Data 开头（扫描器输出的前缀），剥离该段并把定位起点
    跳到 nbt_data["Data"]，保证写回落在正确分支。

    original 传入时（命令方块 Command 的文本参数）：只把值里第一次出现的原文文本
    替换成译文，**保留指令字与结构**（整体替换会把指令翻成中文无法执行，用户实测）。
    """
    parts = _split_path(nbt_path)
    # 去掉首段若为根（Data），同时把下钻起点切到 Data 分支
    if parts and parts[0] == "Data" and len(parts) > 1:
        parts = parts[1:]
        nbt_data = nbt_data["Data"]
    if not parts:
        return
    node = nbt_data
    for p in parts[:-1]:
        node = node[p]
    last = parts[-1]
    if (original is not None and last in node
            and isinstance(node[last], String) and original in str(node[last])):
        raw = str(node[last])
        try:
            obj = json.loads(raw)
            if isinstance(obj, (dict, list)):
                # JSON 组件（{"text":...}）：精确替换 text 字段值 == original 的为译文——
                # 修复：整串 replace 会把「text」键名/重复文本误替换，破坏组件结构
                # （Minecraft 读不出组件 → 告示牌/自定义名汉化失败）
                _replace_component_text(obj, original, translated)
                # separators 无空格，保持原组件格式（对齐 scan 原文，Minecraft 兼容）
                node[last] = String(json.dumps(obj, ensure_ascii=False, separators=(',', ':')))
            else:
                node[last] = String(raw.replace(original, translated, 1))
        except (ValueError, json.JSONDecodeError):
            # 非 JSON（纯文本/指令）：仍只替换第一次出现的原文片段，保留指令字
            node[last] = String(raw.replace(original, translated, 1))
    else:
        _set_value(nbt_data, parts, translated)


def _replace_component_text(obj, original: str, translated: str) -> None:
    """递归 JSON 文本组件：把 text 字段中值 == original 的替换为 translated（精确，不碰键名）。

    遍历所有 text 字段（含 extra 数组嵌套），只替换值完全等于 original 的——避免 naive
    `replace` 命中键名「text」或组件内重复文本导致错位。
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "text" and isinstance(v, str) and v == original:
                obj[k] = translated
            else:
                _replace_component_text(v, original, translated)
    elif isinstance(obj, list):
        for e in obj:
            _replace_component_text(e, original, translated)


def _write_backup(path: Path) -> None:
    """写回前把原文件备份为 <name>.bak（若已存在则跳过）。"""
    bak = path.with_name(path.name + ".bak")
    if not bak.exists():
        shutil.copy2(path, bak)


def _parse_chunk_prefix(nbt_path: str) -> tuple[tuple[int, int] | None, str]:
    """解析 scan 输出的 chunk(x,z) 前缀：'chunk(0,0)[0].Command' → ((0,0), '[0].Command')。

    x/z 为 region 内区块索引（0..31，与 scan_mca 的循环一致）；无前缀返回 (None, 原路径)。
    """
    m = re.match(r"chunk\((-?\d+),(-?\d+)\)(.*)$", nbt_path)
    if not m:
        return None, nbt_path
    return (int(m.group(1)), int(m.group(2))), m.group(3)


def _decompress(payload: bytes, compression: int) -> bytes:
    """按压缩类型解压区块数据：1=gzip、2=zlib、3=无压缩。"""
    if compression == 1:
        return gzip.decompress(payload)
    if compression == 2:
        return zlib.decompress(payload)
    if compression == 3:
        return payload
    raise ValueError(f"未知区块压缩类型: {compression}")


def _parse_region(data: bytes) -> list:
    """解析 region 文件：按偏移表读回全部区块条目（list[1024]）。

    条目为 dict{raw, timestamp} 或 None（偏移表 0 表示区块缺失）。
    raw 为区块原始数据单元（4B 长度 + 1B 压缩类型 + 压缩数据），重建时原样保留。
    """
    if len(data) < 8192:
        raise ValueError("region 文件过短（缺少 8KB 头部）")
    chunks: list = []
    for i in range(1024):
        off = i * 4
        sector_off = int.from_bytes(data[off:off + 3], "big")
        sector_count = data[off + 3]
        ts = int.from_bytes(data[4096 + off:4096 + off + 4], "big")
        if sector_off == 0 and sector_count == 0:
            chunks.append(None)
            continue
        start = sector_off * 4096
        unit = data[start:start + sector_count * 4096]
        # 修复：越界/长度非法时视为缺失跳过，不按截断数据重排（否则会抛 IndexError
        # 或把"存在的区块"重排成缺失，进一步损坏 region 文件）
        if len(unit) < 5:
            chunks.append(None)
            continue
        length = int.from_bytes(unit[:4], "big")
        if length < 1 or 4 + length > len(unit):
            chunks.append(None)
            continue
        chunks.append({"raw": unit[:4 + length], "timestamp": ts})
    return chunks


def _find_entity_list(level, index: int):
    """定位实体列表（新 block_entities / 旧 TileEntities）的第 index 个元素所在列表。

    scan_mca 对两种列表都扫，路径只含 [i] 下标不含列表名；正常区块二者不同时存在，
    优先新格式 block_entities，旧格式 TileEntities 兜底。
    """
    # 修复：entities（实体 CustomName/Name，剧情地图 NPC）也纳入兜底——scan_mca 同时扫
    # block_entities/TileEntities/entities，旧式无前缀路径落到这里时实体名字译文不丢
    for key in ("block_entities", "TileEntities", "entities"):
        lst = level.get(key)
        if isinstance(lst, List) and index < len(lst):
            return lst
    return None


_ENTITY_LIST_NAMES = ("block_entities", "TileEntities", "entities")


def _apply_mca_path(root, rest: str, translated: str, original: str | None = None) -> None:
    """在区块根 Compound 上按剩余路径替换文本。

    rest 形态（修复后）：'block_entities[0].CustomName' / 'entities[0].Name' /
    '[0].CustomName'（旧格式） / 普通键路径。兼容 Level 包裹（≤1.14）与平铺（1.15+）布局。
    original（命令方块文本参数）传入时只替换片段，保留指令。
    """
    parts = _split_path(rest)
    if not parts:
        return
    level = root["Level"] if "Level" in root else root
    if isinstance(parts[0], str) and parts[0] in _ENTITY_LIST_NAMES:
        # 新格式：路径首段为列表名，按名定位（修复：block_entities/entities 并存时
        # 原按下标找第一个列表导致实体译文错位/丢失）
        lst = level.get(parts[0])
        if (isinstance(lst, List) and len(parts) > 1
                and isinstance(parts[1], int) and 0 <= parts[1] < len(lst)):
            _set_value(lst, parts[1:], translated, original)
    elif isinstance(parts[0], int):
        lst = _find_entity_list(level, parts[0])
        if lst is None:
            return
        _set_value(lst, parts, translated, original)
    else:
        _set_value(level, parts, translated, original)


def _rebuild_region(chunks: list) -> bytes:
    """重建 region：区块按新大小紧凑重排（4KB 对齐），重建偏移表 + 时间戳表。

    区块压缩后大小变化会破坏后续 sector 偏移，整 region 重排（保持区块顺序）
    100% 可靠；未修改区块保留原始数据单元字节，压缩类型不变。
    """
    offsets = bytearray(4096)
    sector = 2                      # 前两个 sector 是头部（偏移表 + 时间戳表）
    data_parts: list[bytes] = []
    new_timestamps = bytearray(4096)
    for i in range(1024):
        entry = chunks[i]
        if entry is None:
            continue
        unit = entry["raw"]
        sectors = (len(unit) + 4095) // 4096
        offsets[i * 4:i * 4 + 4] = struct.pack(">I", (sector << 8) | sectors)
        new_timestamps[i * 4:i * 4 + 4] = struct.pack(">I", entry["timestamp"])
        data_parts.append(unit + b"\x00" * (sectors * 4096 - len(unit)))
        sector += sectors
    header = bytearray(8192)
    header[:4096] = offsets
    header[4096:] = new_timestamps
    return bytes(header) + b"".join(data_parts)


def write_mca(file: Path, translations: list[dict]) -> None:
    """把译文写回 .mca region：整 region 重写（读全部区块 → 目标替换 → 重建）。

    translations 元素含 nbt_path（形如 chunk(x,z)[i].Command，x/z 为 region 内 0..31 索引）。
    目标区块解压后用 nbtlib 替换（复用 _split_path/_set_value），未目标区块原字节保留；
    全部区块紧凑重排 + 4KB 对齐，重建偏移表/时间戳表。写前备份 <name>.bak。
    """
    chunks = _parse_region(file.read_bytes())
    by_index: dict[int, list] = {}
    for t in translations:
        coords, rest = _parse_chunk_prefix(t["nbt_path"])
        if coords is None:
            continue
        cx, cz = coords
        if not (0 <= cx < 32 and 0 <= cz < 32):
            continue
        by_index.setdefault(cx + cz * 32, []).append((t, rest))
    for idx, items in by_index.items():
        entry = chunks[idx]
        if entry is None:
            continue    # 目标区块缺失：与 scan_mca 跳过缺失区块一致，忽略该区块词条
        raw = entry["raw"]
        length = int.from_bytes(raw[:4], "big")
        compression = raw[4]
        payload = raw[5:5 + length - 1]
        try:
            root = File.parse(io.BytesIO(_decompress(payload, compression)))
        except Exception:
            continue    # 区块 NBT 损坏：保留原字节，跳过该区块替换
        for t, rest in items:
            _apply_mca_path(root, rest, t["translated"], original=t["text"])
        buf = io.BytesIO()
        File.write(root, buf)
        new_payload = zlib.compress(buf.getvalue(), 6)
        entry["raw"] = struct.pack(">I", len(new_payload) + 1) + b"\x02" + new_payload
    _write_backup(file)
    file.write_bytes(_rebuild_region(chunks))


def write_translations(file: Path, translations: list[dict]) -> None:
    """把译文写回 .dat/.json/.mcfunction/.mca。写前备份。"""
    if not translations:
        return
    _write_backup(file)
    suffix = file.suffix.lower()
    if suffix == ".dat":
        nbt = File.load(file, gzipped=True)
        for t in translations:
            apply_translation(nbt, t["nbt_path"], t["translated"], original=t["text"])
        nbt.save(file, gzipped=True)
    elif suffix == ".mca":
        write_mca(file, translations)
    elif suffix == ".json":
        data = json.loads(file.read_text(encoding="utf-8"))
        flat = all(isinstance(v, str) for v in data.values())
        for t in translations:
            if flat:
                # 扁平 lang 文件（地图自带资源包 assets/*/lang/*.json 的通用形态）：
                # 键名可含点（如 "key.hello"），必须整键替换，不能 split 当下钻路径
                # （原逻辑把 key.hello 拆成两层下钻 → 对 str 取下标 TypeError → 任务崩）
                if t["nbt_path"] in data:
                    data[t["nbt_path"]] = t["translated"]
                continue
            parts = _split_path(t["nbt_path"])
            # 嵌套结构（数据包/新版 translations 等）：沿路径下钻；路径不匹配跳过该条不崩
            try:
                node = data
                for p in parts[:-1]:
                    node = node[p]
                if isinstance(parts[-1], str) and parts[-1] in node:
                    node[parts[-1]] = t["translated"]
            except (KeyError, TypeError, IndexError):
                continue   # 结构不匹配：保留原样，跳过该条
        file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    elif suffix == ".mcfunction":
        lines = file.read_text(encoding="utf-8").splitlines()
        for t in translations:
            m = re.fullmatch(r"line(\d+)", t["nbt_path"])
            if m:
                idx = int(m.group(1))
                if 0 <= idx < len(lines):
                    lines[idx] = lines[idx].replace(t["text"], t["translated"], 1)
        file.write_text("\n".join(lines) + "\n", encoding="utf-8")
