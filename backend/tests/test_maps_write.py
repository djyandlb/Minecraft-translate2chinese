"""M4-4 地图副本写回测试。"""
import io
import struct
import zlib
from pathlib import Path

from nbtlib import Compound, File, Int, List, String
from app.maps.write import _split_path, apply_translation, write_translations


def _build_region(chunks: dict) -> bytes:
    """构造最小 region 字节：chunks 为 {区域索引: 未压缩区块 NBT 字节}。

    每个区块 zlib 压缩（type=2，与真实存档一致），从 sector 2 起紧凑排布，
    重建偏移表（3B sector 偏移 + 1B 长度）与时间戳表（全 0）。
    """
    offsets = bytearray(4096)
    sector = 2                      # 前两个 sector 是头部（偏移表 + 时间戳表）
    parts: list[bytes] = []
    for i in range(1024):
        nbt = chunks.get(i)
        if nbt is None:
            continue
        payload = zlib.compress(nbt, 6)
        unit = struct.pack(">I", len(payload) + 1) + b"\x02" + payload
        sectors = (len(unit) + 4095) // 4096
        offsets[i * 4:i * 4 + 4] = struct.pack(">I", (sector << 8) | sectors)
        parts.append(unit + b"\x00" * (sectors * 4096 - len(unit)))
        sector += sectors
    header = bytearray(8192)
    header[:4096] = offsets
    return bytes(header) + b"".join(parts)


def _modern_chunk(command: str | None = None, sign: str | None = None,
                  x_pos: int = 0, z_pos: int = 0) -> bytes:
    """构造现代区块 NBT（平铺布局，1.15+）字节，含可选命令方块/告示牌 block_entities。"""
    bes = []
    if command is not None:
        bes.append(Compound({
            "id": String("minecraft:command_block"),
            "Command": String(command),
            "x": Int(0), "y": Int(60), "z": Int(0),
        }))
    if sign is not None:
        bes.append(Compound({
            "id": String("minecraft:oak_sign"),
            "front_text": Compound({"messages": List[String]([sign, "zzz"])}),
            "x": Int(1), "y": Int(60), "z": Int(0),
        }))
    root = Compound({"DataVersion": Int(3465), "xPos": Int(x_pos), "zPos": Int(z_pos)})
    if bes:
        root["block_entities"] = List[Compound](bes)
    # region 区块 NBT 根是具名复合体（0x0a + 空根名 + 字段），用 File 写出以对齐真实存档
    f = File(root)
    buf = io.BytesIO()
    File.write(f, buf)
    return buf.getvalue()


def _read_region_chunks(data: bytes) -> list:
    """按偏移表读回 region 全部区块的解压 NBT 字节（None 表示缺失）。"""
    out = []
    for i in range(1024):
        entry = data[i * 4:i * 4 + 4]
        sector_off = int.from_bytes(entry[:3], "big")
        n = entry[3]
        if sector_off == 0 and n == 0:
            out.append(None)
            continue
        start = sector_off * 4096
        length = int.from_bytes(data[start:start + 4], "big")
        comp = data[start + 4]
        payload = data[start + 5:start + 5 + length - 1]
        if comp == 2:
            out.append(zlib.decompress(payload))
        elif comp == 1:
            import gzip
            out.append(gzip.decompress(payload))
        else:
            out.append(payload)
    return out


def test_apply_translation_nested_path(tmp_path: Path):
    nbt = File({"Data": Compound({"Command": String("say Hello")})})
    apply_translation(nbt["Data"], "Command", "说：你好")
    assert str(nbt["Data"]["Command"]) == "说：你好"


def test_apply_translation_list_index(tmp_path: Path):
    from nbtlib import List as NbtList
    nbt = File({"Data": Compound({"pages": NbtList([String("old"), String("second")])})})
    apply_translation(nbt["Data"], "pages[0]", "新内容")
    assert str(nbt["Data"]["pages"][0]) == "新内容"


def test_write_translations_dat(tmp_path: Path):
    p = tmp_path / "x.dat"
    File({"Data": Compound({"Command": String("say Hello")})}).save(p, gzipped=True)
    write_translations(p, [{"nbt_path": "Data.Command", "text": "say Hello", "translated": "说：你好"}])
    loaded = File.load(p, gzipped=True)
    assert str(loaded["Data"]["Command"]) == "说：你好"
    assert (tmp_path / "x.dat.bak").exists()   # 备份已建


def test_write_customname_json_component(tmp_path):
    """JSON 组件（CustomName={"text":...}）写回：只替换组件内 text 值，保留 JSON 结构。"""
    p = tmp_path / "x.dat"
    File({"Data": Compound({"CustomName": String('{"text":"Guard NPC","color":"green"}')})}).save(p, gzipped=True)
    write_translations(p, [{"nbt_path": "Data.CustomName", "text": "Guard NPC", "translated": "守卫NPC"}])
    loaded = File.load(p, gzipped=True)
    assert str(loaded["Data"]["CustomName"]) == '{"text":"守卫NPC","color":"green"}'


def test_write_translations_json_text(tmp_path: Path):
    import json
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"title": {"text": "old text"}}), encoding="utf-8")
    write_translations(p, [{"nbt_path": "title.text", "text": "old text", "translated": "新标题"}])
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["title"]["text"] == "新标题"


def test_write_translations_flat_lang_json(tmp_path: Path):
    """地图自带资源包 lang 文件（扁平 dict，键含点）→ 整键替换，不崩、不破坏其他条目。"""
    import json
    p = tmp_path / "assets" / "map" / "lang" / "zh_cn.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"map.title": "Old Map Title", "map.button": "Start"}),
                 encoding="utf-8")
    write_translations(p, [{"nbt_path": "map.title", "text": "Old Map Title", "translated": "新地图标题"}])
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["map.title"] == "新地图标题"     # 键含点也整键替换
    assert data["map.button"] == "Start"        # 未翻译条目保留原样


def test_write_translations_nested_mismatch_no_crash(tmp_path: Path):
    """嵌套 json 路径不匹配（新版 translations 结构等）→ 跳过该条不崩。"""
    import json
    p = tmp_path / "data.json"
    p.write_text(json.dumps({"translations": {"a.b": "v"}}), encoding="utf-8")
    write_translations(p, [{"nbt_path": "a.b", "text": "v", "translated": "译"}])
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["translations"]["a.b"] == "v"   # 结构不匹配 → 保留原样


# —— .mca region 写回（M4-mca）——

def test_split_path_leading_index():
    """[i] 开头的列表路径解析（.mca 的 block_entities 下标）。"""
    assert _split_path("[0].Command") == [0, "Command"]
    assert _split_path("a.b[0].c") == ["a", "b", 0, "c"]
    assert _split_path("[0].front_text.messages[1]") == [0, "front_text", "messages", 1]


def test_write_mca_replaces_command(tmp_path: Path):
    """命令方块文本写回：目标区块被替换，其他区块与偏移表不被破坏。"""
    p = tmp_path / "r.0.0.mca"
    p.write_bytes(_build_region({
        0: _modern_chunk(command="say Hello from mca"),
        1: _modern_chunk(command="say keep me", x_pos=1),
    }))
    write_translations(p, [{"nbt_path": "chunk(0,0)[0].Command", "text": "say Hello from mca",
                            "translated": "说：区块你好"}])
    reread = _read_region_chunks(p.read_bytes())
    root0 = File.parse(io.BytesIO(reread[0]))
    root1 = File.parse(io.BytesIO(reread[1]))
    assert str(root0["block_entities"][0]["Command"]) == "说：区块你好"
    assert str(root1["block_entities"][0]["Command"]) == "say keep me"   # 未目标区块保持原样
    assert (tmp_path / "r.0.0.mca.bak").exists()                          # 写前已备份


def test_write_mca_sign_messages(tmp_path: Path):
    """区块内告示牌（front_text.messages 列表）写回。"""
    p = tmp_path / "r.0.0.mca"
    p.write_bytes(_build_region({0: _modern_chunk(sign="Hello sign reader")}))
    write_translations(p, [{"nbt_path": "chunk(0,0)[0].front_text.messages[0]",
                            "text": "Hello sign reader", "translated": "你好，告示牌"}])
    root = File.parse(io.BytesIO(_read_region_chunks(p.read_bytes())[0]))
    assert str(root["block_entities"][0]["front_text"]["messages"][0]) == "你好，告示牌"
    assert str(root["block_entities"][0]["front_text"]["messages"][1]) == "zzz"  # 其他元素不动


def test_write_mca_old_level_tile_entities(tmp_path: Path):
    """旧格式（Level 包裹 + TileEntities，≤1.14）写回兼容。"""
    inner = Compound({
        "DataVersion": Int(1343),
        "TileEntities": List[Compound]([Compound({
            "id": String("minecraft:command_block"),
            "Command": String("say old style"),
        })]),
    })
    root = Compound({"Level": inner})
    f = File(root)
    buf = io.BytesIO()
    File.write(f, buf)
    p = tmp_path / "r.0.0.mca"
    p.write_bytes(_build_region({0: buf.getvalue()}))
    write_translations(p, [{"nbt_path": "chunk(0,0)[0].Command", "text": "say old style",
                            "translated": "说：旧格式"}])
    root2 = File.parse(io.BytesIO(_read_region_chunks(p.read_bytes())[0]))
    assert str(root2["Level"]["TileEntities"][0]["Command"]) == "说：旧格式"


def test_write_mca_rebuild_layout_correct(tmp_path: Path):
    """重建后偏移表仍能定位每个区块（数据不因紧凑重排丢失）。"""
    p = tmp_path / "r.0.0.mca"
    p.write_bytes(_build_region({
        0: _modern_chunk(command="say A"),
        2: _modern_chunk(command="say B", x_pos=2),
        5: _modern_chunk(command="say C", x_pos=5),
    }))
    write_translations(p, [{"nbt_path": "chunk(0,0)[0].Command", "text": "say A",
                            "translated": "说：A"}])
    reread = _read_region_chunks(p.read_bytes())
    assert reread[0] is not None and reread[2] is not None and reread[5] is not None
    assert reread[1] is None and reread[3] is None and reread[4] is None
    for idx in (0, 2, 5):
        root = File.parse(io.BytesIO(reread[idx]))
        assert str(root["block_entities"][0]["Command"]).startswith(("说：", "say"))


def test_write_translations_mca_branch(tmp_path: Path):
    """write_translations 的 .mca 分支入口（无翻译时直接返回，不备份）。"""
    p = tmp_path / "r.0.0.mca"
    p.write_bytes(_build_region({0: _modern_chunk(command="say x")}))
    write_translations(p, [])
    assert not (tmp_path / "r.0.0.mca.bak").exists()
