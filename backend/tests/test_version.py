from app.version import version_to_pack_format, pack_format_to_lang_ext

def test_known_versions():
    assert version_to_pack_format("1.20.1") == 15
    assert version_to_pack_format("1.12.2") == 3

def test_lang_ext_boundary():
    assert pack_format_to_lang_ext(3) == "lang"   # 1.12
    assert pack_format_to_lang_ext(4) == "json"   # 1.13+
    assert pack_format_to_lang_ext(15) == "json"

def test_unknown_version_fallback():
    assert version_to_pack_format("9.9.9") == 15
