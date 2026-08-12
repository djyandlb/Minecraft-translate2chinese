"""M4-3 地图 NBT 扫描测试。"""
import json
from pathlib import Path
from nbtlib import File, Compound, String
from app.maps.scan import load_scan_keys, is_translatable_text, scan_nbt, scan_dat, scan_json_text, scan_mcfunction, scan_file, scan_world


def test_load_scan_keys(tmp_path: Path):
    p = tmp_path / "keys.json"
    p.write_text('["Command", "CustomName"]', encoding="utf-8")
    assert load_scan_keys(p) == {"Command", "CustomName"}


def test_is_translatable_text():
    assert is_translatable_text("say Hello world")
    assert not is_translatable_text("iron_ingot")
    assert not is_translatable_text("12345")


def _make_dat(path: Path, entries: dict):
    # nbtlib 2.0.4 的 Compound 值需为 tag 类型，裸 str 保存时无 tag_id
    data = Compound({k: String(v) for k, v in entries.items()})
    File({"Data": data}).save(path, gzipped=True)


def test_scan_dat_hits_command(tmp_path: Path):
    p = tmp_path / "x.dat"
    _make_dat(p, {"Command": "say Hello world", "Unrelated": "skip me"})
    hits = scan_dat(p, {"Command"})
    # 只收集指令的文本参数（say 后的引号文本），不整体翻译指令字
    assert len(hits) == 1 and hits[0]["text"] == "Hello world"
    # M4-6：scan_world 给每个 entry 补 file 字段，写回需要知道目标文件
    world_hits = scan_world(tmp_path)
    assert world_hits and world_hits[0]["file"] == str(p)


def test_scan_nbt_pages_list_of_string(tmp_path):
    from nbtlib import List as NbtList
    p = tmp_path / "book.dat"
    File({"Data": Compound({"pages": NbtList([String("Chapter one begins"), String("And then it ends")])})}).save(p, gzipped=True)
    hits = scan_dat(p, {"pages"})
    texts = [h["text"] for h in hits]
    assert "Chapter one begins" in texts and "And then it ends" in texts


def test_scan_nbt_front_text_compound(tmp_path):
    from nbtlib import List as NbtList
    p = tmp_path / "sign.dat"
    ft = Compound({"messages": NbtList([String("Hello sign reader")])})
    File({"Data": Compound({"front_text": ft})}).save(p, gzipped=True)
    hits = scan_dat(p, {"front_text"})
    assert any("Hello sign reader" in h["text"] for h in hits)


def test_scan_json_text(tmp_path: Path):
    p = tmp_path / "advancement.json"
    p.write_text(json.dumps({"title": {"text": "Welcome to the world"}, "key2": {"text": "iron_ingot"}}), encoding="utf-8")
    hits = scan_json_text(p, set())
    assert any(h["text"] == "Welcome to the world" for h in hits)
    assert not any(h["text"] == "iron_ingot" for h in hits)   # 技术串过滤


def test_scan_mcfunction(tmp_path: Path):
    p = tmp_path / "a.mcfunction"
    p.write_text('say Hello there\ntellraw @a {"text":"Greetings player"}\n', encoding="utf-8")
    hits = scan_mcfunction(p, set())
    assert any("Hello there" in h["text"] for h in hits)
    assert any("Greetings player" in h["text"] for h in hits)   # tellraw JSON 提取整段 text


def test_scan_mcfunction_tellraw_extra(tmp_path: Path):
    p = tmp_path / "b.mcfunction"
    p.write_text(
        'tellraw @a {"text":"Prefix","extra":[{"text":"Middle"},{"text":"tail"}]}\n'
        'title @a title {"text":"Pure title"}\n',
        encoding="utf-8",
    )
    hits = scan_mcfunction(p, set())
    texts = [h["text"] for h in hits]
    assert "Prefix" in texts and "Middle" in texts and "tail" in texts
    assert "Pure title" in texts   # title 的 JSON 参数同样被解析


def _build_mca_bytes(chunk_nbt: bytes, cx: int = 0, cz: int = 0) -> bytes:
    """把 zlib 压缩的 chunk NBT 打包成最小 .mca 字节（供测试用）。

    头部 4096 字节存 chunk 位置表，chunk 数据从 sector 1 起，
    压缩类型固定 2（zlib，与真实存档一致）。
    """
    import struct
    payload = struct.pack(">I", len(chunk_nbt) + 1) + b"\x02" + chunk_nbt
    padded = payload + b"\x00" * ((4096 - len(payload) % 4096) % 4096)
    sectors = len(padded) // 4096
    header = bytearray(4096)
    index = (cx % 32) + (cz % 32) * 32
    header[index * 4:index * 4 + 4] = struct.pack(">I", (1 << 8) | sectors)
    return bytes(header) + padded


def _make_modern_mca(path: Path):
    """生成含现代 block_entities（命令方块）的 .mca 副本文件。"""
    import io
    import zlib
    from nbt.nbt import NBTFile, TAG_Compound, TAG_Int, TAG_List, TAG_String

    root = NBTFile()
    root.tags.append(TAG_Int(name="DataVersion", value=3465))
    bes = TAG_List(name="block_entities", type=TAG_Compound)
    cmd = TAG_Compound()
    cmd.tags.extend([
        TAG_String(name="id", value="minecraft:command_block"),
        TAG_String(name="Command", value="say Hello from mca"),
        TAG_Int(name="x", value=0), TAG_Int(name="y", value=60), TAG_Int(name="z", value=0),
    ])
    bes.tags.append(cmd)
    root.tags.extend([
        TAG_Int(name="xPos", value=0), TAG_Int(name="zPos", value=0),
        bes,
    ])
    buf = io.BytesIO()
    root.write_file(buffer=buf)
    path.write_bytes(_build_mca_bytes(zlib.compress(buf.getvalue())))


def _make_entities_mca(path: Path):
    """生成含 entities 列表（实体 CustomName，剧情地图 NPC）的 .mca 副本。"""
    import io
    import zlib
    from nbt.nbt import NBTFile, TAG_Compound, TAG_Int, TAG_List, TAG_String

    root = NBTFile()
    root.tags.append(TAG_Int(name="DataVersion", value=3465))
    ents = TAG_List(name="entities", type=TAG_Compound)
    ent = TAG_Compound()
    ent.tags.extend([
        TAG_String(name="id", value="minecraft:armor_stand"),
        TAG_String(name="CustomName", value="Guard Captain"),
    ])
    ents.tags.append(ent)
    root.tags.extend([
        TAG_Int(name="xPos", value=0), TAG_Int(name="zPos", value=0),
        ents,
    ])
    buf = io.BytesIO()
    root.write_file(buffer=buf)
    path.write_bytes(_build_mca_bytes(zlib.compress(buf.getvalue())))


def test_scan_mca_block_entities(tmp_path: Path):
    p = tmp_path / "r.0.0.mca"
    _make_modern_mca(p)
    hits = scan_file(p, {"Command"})
    # 命令方块只收集 say 的文本参数（保留指令字，不整体翻译）
    assert any(h["text"] == "Hello from mca" for h in hits)


def test_scan_customname_json_component(tmp_path):
    """CustomName 是 JSON 组件（{"text":"NPC"}）→ 只收 text 值，不整体收集 JSON 串（会坏组件）。"""
    p = tmp_path / "x.dat"
    _make_dat(p, {"CustomName": '{"text":"Guard NPC","color":"green"}', "Unrelated": "skip"})
    hits = scan_dat(p, {"CustomName"})
    assert any(h["text"] == "Guard NPC" for h in hits)
    assert not any(h["text"].lstrip().startswith("{") for h in hits)   # 不整体收集 JSON 串


def test_scan_mca_entities_customname(tmp_path: Path):
    """剧情地图实体 NPC 名（CustomName）也被扫描（此前只扫 block_entities，漏 NPC 名）。"""
    p = tmp_path / "r.0.0.mca"
    _make_entities_mca(p)
    hits = scan_file(p, {"CustomName"})
    assert any("Guard Captain" in h["text"] for h in hits)


def test_scan_file_mca_empty_on_missing(tmp_path: Path):
    p = tmp_path / "r.0.0.mca"
    # 非法 .mca 字节：不崩溃，返回空
    p.write_bytes(b"\x00" * 4096)
    assert scan_file(p, {"Command"}) == []
