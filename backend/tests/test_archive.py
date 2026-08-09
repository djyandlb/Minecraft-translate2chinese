import zipfile
from pathlib import Path
from app.archive import is_archive, extract_modpack

def test_is_archive():
    assert is_archive(Path("a.zip"))
    assert is_archive(Path("pack.mrpack"))
    assert not is_archive(Path("a.jar"))
    assert not is_archive(Path("a_folder"))

def test_extract_modpack(tmp_path: Path):
    src = tmp_path / "pack.mrpack"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("mods/m1.jar", "x")
        zf.writestr("modrinth.index.json", "{}")
    dest = tmp_path / "out"
    root = extract_modpack(src, dest)
    assert (root / "mods" / "m1.jar").exists()
