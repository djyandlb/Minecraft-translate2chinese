"""M4-4 地图副本写回模块。

消费 scan.py 产出的 {text, nbt_path, key, file} 结构，把译文写回世界副本的
NBT / JSON / mcfunction 文件。写前把原文件备份为 <name>.bak（若不存在）。
全程 pathlib 路径，只写副本，不碰原档。
"""
import json
import re
import shutil
from pathlib import Path

from nbtlib import Compound, File, List, String


def _split_path(nbt_path: str) -> list:
    """'a.b[0].c' → ['a', 0, 'c']；[i] 转 int 下标。"""
    parts: list = []
    for seg in nbt_path.split("."):
        m = re.fullmatch(r"(\w+)(?:\[(\d+)\])?", seg)
        if not m:
            continue
        parts.append(m.group(1))
        if m.group(2) is not None:
            parts.append(int(m.group(2)))
    return parts


def _set_value(data, parts: list, translated: str) -> None:
    """沿路径下钻到最后一段，赋新的 String 值。"""
    node = data
    for p in parts[:-1]:
        node = node[p]
    # nbtlib 的 Compound 键与 List 下标均支持赋值；List 下标赋 String 自动入列
    node[parts[-1]] = String(translated)


def apply_translation(nbt_data, nbt_path: str, translated: str) -> None:
    """按路径替换 NBT String 值。路径形如 Data.Command、Data.pages[0]。

    若路径以根段 Data 开头（扫描器输出的前缀），剥离该段并把定位起点
    跳到 nbt_data["Data"]，保证写回落在正确分支。
    """
    parts = _split_path(nbt_path)
    # 去掉首段若为根（Data），同时把下钻起点切到 Data 分支
    if parts and parts[0] == "Data" and len(parts) > 1:
        parts = parts[1:]
        nbt_data = nbt_data["Data"]
    if parts:
        _set_value(nbt_data, parts, translated)


def _write_backup(path: Path) -> None:
    """写回前把原文件备份为 <name>.bak（若已存在则跳过）。"""
    bak = path.with_name(path.name + ".bak")
    if not bak.exists():
        shutil.copy2(path, bak)


def write_translations(file: Path, translations: list[dict]) -> None:
    """把译文写回 .dat/.json/.mcfunction。写前备份。"""
    if not translations:
        return
    _write_backup(file)
    suffix = file.suffix.lower()
    if suffix == ".dat":
        nbt = File.load(file, gzipped=True)
        for t in translations:
            apply_translation(nbt, t["nbt_path"], t["translated"])
        nbt.save(file, gzipped=True)
    elif suffix == ".json":
        data = json.loads(file.read_text(encoding="utf-8"))
        for t in translations:
            parts = _split_path(t["nbt_path"])
            node = data
            for p in parts[:-1]:
                node = node[p]
            if isinstance(parts[-1], str) and parts[-1] in node:
                node[parts[-1]] = t["translated"]
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
