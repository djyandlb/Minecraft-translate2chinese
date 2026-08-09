from app.translate.han import simplify, traditional, is_same_script

def test_traditional():
    assert traditional("铁锭") == "鐵錠"

def test_simplify():
    assert simplify("鐵錠") == "铁锭"

def test_is_same_script():
    assert is_same_script("zh_cn", "zh_tw")
    assert is_same_script("zh_tw", "zh_cn")
    assert not is_same_script("en_us", "zh_cn")
