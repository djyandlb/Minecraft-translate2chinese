"""M4-mca 地图翻译流程测试：.mca 区块文本不再被过滤，进入翻译并写回 mcworld 产物。"""
import io
import struct
import zipfile
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from nbtlib import Compound, File, Int, List, String
from app.maps.flow import run_map_translation
from app.tasks import TaskStore


class _FakeEngine:
    """假翻译引擎：任意文本 → 加【译】前缀。"""

    async def translate_batch(self, texts, target_lang):
        return ["【译】" + t for t in texts]


def _modern_chunk(command: str) -> bytes:
    """含命令方块的现代区块 NBT 字节。"""
    root = Compound({
        "DataVersion": Int(3465),
        "xPos": Int(0),
        "zPos": Int(0),
        "block_entities": List[Compound]([Compound({
            "id": String("minecraft:command_block"),
            "Command": String(command),
            "x": Int(0), "y": Int(60), "z": Int(0),
        })]),
    })
    # region 区块 NBT 根是具名复合体（0x0a + 空根名 + 字段），用 File 写出以对齐真实存档
    f = File(root)
    buf = io.BytesIO()
    File.write(f, buf)
    return buf.getvalue()


def _build_region(chunks: dict) -> bytes:
    """构造最小 region 字节（zlib 压缩、4KB 对齐、重建偏移表）。"""
    offsets = bytearray(4096)
    sector = 2
    parts: list[bytes] = []
    for i in range(1024):
        nbt = chunks.get(i)
        if nbt is None:
            continue
        payload = zlib.compress(nbt, 6)
        unit = struct.pack(">I", len(payload) + 1) + b"\x02" + payload
        n = (len(unit) + 4095) // 4096
        offsets[i * 4:i * 4 + 4] = struct.pack(">I", (sector << 8) | n)
        parts.append(unit + b"\x00" * (n * 4096 - len(unit)))
        sector += n
    header = bytearray(8192)
    header[:4096] = offsets
    return bytes(header) + b"".join(parts)


def _chunk0_nbt(mca: bytes) -> File:
    """读 .mca 第 0 个区块并返回 nbtlib File（根名感知）。"""
    off = int.from_bytes(mca[:3], "big") * 4096
    length = int.from_bytes(mca[off:off + 4], "big")
    payload = mca[off + 5:off + 5 + length - 1]
    return File.parse(io.BytesIO(zlib.decompress(payload)))


@pytest.mark.asyncio
async def test_flow_translates_mca_command(tmp_path, monkeypatch):
    """命令方块文本进入翻译并写回：mcworld 产物里 .mca 的 Command 带译文。"""
    world = tmp_path / "world"
    (world / "region").mkdir(parents=True)
    from nbtlib import File
    File({"Data": Compound({"LevelName": String("t")})}).save(world / "level.dat", gzipped=True)
    (world / "region" / "r.0.0.mca").write_bytes(
        _build_region({0: _modern_chunk("say Hello from mca")}))

    monkeypatch.setattr("app.maps.flow.create_engine", lambda cfg: _FakeEngine())
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    outputs = tmp_path / "outputs"
    req = SimpleNamespace(path=str(world), source_lang="en", target_lang="zh_cn")
    await run_map_translation(state.id, req, None, store, work, outputs)

    assert store.load(state.id).status == "done"
    assert store.load(state.id).total >= 1   # .mca 词条不再被过滤
    out = outputs / f"{state.id}_zh_cn.mcworld"
    assert out.exists()
    with zipfile.ZipFile(out) as zf:
        data = zf.read("region/r.0.0.mca")
    root = _chunk0_nbt(data)
    # 只替换指令的文本参数（say 后的文本），保留指令字——整体翻译会让指令无法执行
    assert str(root["block_entities"][0]["Command"]) == "say 【译】Hello from mca"
