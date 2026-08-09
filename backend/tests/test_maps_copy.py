from pathlib import Path
from app.maps.copy import copy_world

def _make_world(path: Path):
    path.mkdir(parents=True)
    (path / "level.dat").write_bytes(b"level-data-bytes")
    (path / "region").mkdir()
    (path / "region" / "r.0.0.mca").write_bytes(b"mca-bytes")

def test_copy_world_copies_all(tmp_path: Path):
    src = tmp_path / "src"
    _make_world(src)
    dest = tmp_path / "copy"
    out = copy_world(src, dest)
    assert out == dest
    assert (dest / "level.dat").read_bytes() == b"level-data-bytes"
    assert (dest / "region" / "r.0.0.mca").exists()

def test_copy_world_overwrites_existing(tmp_path: Path):
    src = tmp_path / "src"
    _make_world(src)
    dest = tmp_path / "copy"
    dest.mkdir()
    (dest / "stale.txt").write_text("旧残留", encoding="utf-8")
    copy_world(src, dest)
    assert not (dest / "stale.txt").exists()   # 旧内容被清掉

def test_copy_world_missing_src(tmp_path: Path):
    import pytest
    with pytest.raises(FileNotFoundError):
        copy_world(tmp_path / "nope", tmp_path / "dest")
