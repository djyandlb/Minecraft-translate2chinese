from pathlib import Path
from nbtlib import File, Compound, String
from app.maps.world import validate_world, get_world_info, list_world_files


def _make_world(path: Path, name: str = "Test World"):
    """构造一个最小可用的世界存档目录。"""
    path.mkdir(parents=True)
    nbt = File({"Data": Compound({"LevelName": String(name)})})
    nbt.save(path / "level.dat", gzipped=True)


def test_validate_world(tmp_path: Path):
    _make_world(tmp_path / "w")
    assert validate_world(tmp_path / "w") is True


def test_validate_world_missing(tmp_path: Path):
    assert validate_world(tmp_path / "empty") is False


def test_get_world_info(tmp_path: Path):
    _make_world(tmp_path / "w")
    info = get_world_info(tmp_path / "w")
    assert info["name"] == "Test World"


def test_list_world_files(tmp_path: Path):
    w = tmp_path / "w"
    _make_world(w)
    (w / "data").mkdir(parents=True)
    (w / "data" / "x.mca").write_bytes(b"x")
    files = list_world_files(w)
    assert any(f.name == "level.dat" for f in files)
    assert any(f.suffix == ".mca" for f in files)
