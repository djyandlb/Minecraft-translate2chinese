from app.version import (version_to_pack_format, pack_format_to_lang_ext,
                         pack_format_spec)

def test_known_versions():
    assert version_to_pack_format("1.20.1") == 15
    assert version_to_pack_format("1.12.2") == 3
    # 修复（recheck）：1.16.2-1.19.4 / 1.21.2-1.21.8 两段曾整体错位（产物被游戏拒载）
    assert version_to_pack_format("1.16.2") == 6
    assert version_to_pack_format("1.17.1") == 7
    assert version_to_pack_format("1.18.2") == 8
    assert version_to_pack_format("1.19.2") == 9
    assert version_to_pack_format("1.19.3") == 12
    assert version_to_pack_format("1.19.4") == 13
    assert version_to_pack_format("1.21.2") == 42
    assert version_to_pack_format("1.21.4") == 46
    assert version_to_pack_format("1.21.5") == 55
    assert version_to_pack_format("1.21.7") == 64

def test_new_array_format_1_21_9():
    # 1.21.9+（25w31a 起）：pack_format 为 major.minor 数组
    assert version_to_pack_format("1.21.9") == 69
    assert version_to_pack_format("1.21.10") == 69
    assert pack_format_spec("1.21.9") == [69, 0]
    assert pack_format_spec("1.21.10") == [69, 0]
    assert pack_format_spec("1.20.6") == 32      # 旧版返回整数

def test_lang_ext_boundary():
    assert pack_format_to_lang_ext(3) == "lang"   # 1.12
    assert pack_format_to_lang_ext(4) == "json"   # 1.13+
    assert pack_format_to_lang_ext(15) == "json"
    assert pack_format_to_lang_ext(69) == "json"  # 1.21.9+ 数组格式的整数部分

def test_unknown_version_fallback():
    assert version_to_pack_format("9.9.9") == 15
