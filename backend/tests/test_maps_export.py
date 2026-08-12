"""M4-5 地图导出测试。"""
import zipfile
from pathlib import Path
from app.maps.export import export_world


def _make_world(path: Path):
    path.mkdir(parents=True)
    (path / "level.dat").write_bytes(b"level-data")
    (path / "region").mkdir()
    (path / "region" / "r.0.0.mca").write_bytes(b"mca")


def test_export_world_zips(tmp_path: Path):
    w = tmp_path / "world"
    _make_world(w)
    out = tmp_path / "out" / "world.mcworld"
    result = export_world(w, out)
    assert result == out and out.exists()
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert "level.dat" in names
        assert "region/r.0.0.mca" in names
