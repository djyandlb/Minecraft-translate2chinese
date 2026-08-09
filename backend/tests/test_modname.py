# mod 中文名推断 + 友好产物命名测试
import json
import zipfile

from app.modname import (friendly_output_name, original_project_label,
                         resolve_mod_name, usable_chinese_name)

_EMPTY_ZIP = b"PK\x05\x06" + b"\x00" * 18


def test_original_label_strips_version():
    """文件名清洗去版本号：mc1.20.1 / 1.20.1-0.3.5 被剥掉。"""
    assert original_project_label("mymod-mc1.20.1-0.3.5.jar") == "mymod"
    assert original_project_label("mymod-1.20.1.jar") == "mymod"
    assert original_project_label("mymod-1.20.1-forge.jar") == "mymod"
    # 无信息量文件名不产生 label
    assert original_project_label("mod.jar") is None
    assert original_project_label("download.jar") is None


def test_usable_chinese_name():
    """验证函数：含 CJK 且 ≥2 字且非通用名。"""
    assert usable_chinese_name("农夫乐事") is True
    assert usable_chinese_name("Macaw 的窗户") is True
    assert usable_chinese_name("a") is False          # 过短
    assert usable_chinese_name("mymod") is False      # 无中文
    assert usable_chinese_name("模组") is False       # 通用名
    assert usable_chinese_name("") is False


def test_resolve_mod_name_known(tmp_path):
    """known 表命中：mod id mcwwindows → Macaw 的窗户。"""
    jar = tmp_path / "macaws-windows-mc1.20.1-0.3.5.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("fabric.mod.json",
                    json.dumps({"id": "mcwwindows", "name": "Macaw's Windows"}))
    assert resolve_mod_name(jar) == "Macaw 的窗户"


def test_resolve_mod_name_filename_fallback(tmp_path):
    """无元数据无 known：文件名清洗回退原 stem 名。"""
    jar = tmp_path / "mymod-mc1.20.1-0.3.5.jar"
    jar.write_bytes(_EMPTY_ZIP)
    assert resolve_mod_name(jar) == "mymod"


def test_resolve_mod_name_embedded_chinese(tmp_path):
    """jar 内已有中文名（fabric.mod.json name）优先。"""
    jar = tmp_path / "whatever-mc1.20.1.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("fabric.mod.json",
                    json.dumps({"id": "mymod", "name": "我的奇妙模组"}))
    assert resolve_mod_name(jar) == "我的奇妙模组"


def test_resolve_mod_name_toml_display(tmp_path):
    """neoforge.mods.toml displayName 中中文名被提取。"""
    jar = tmp_path / "forge-mod.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("META-INF/mods.toml",
                    "modLoader='javafml'\n[[mods]]\nmodId='m'\ndisplayName='农夫乐事'\n")
    assert resolve_mod_name(jar) == "农夫乐事"


def test_resolve_mod_name_english_translate(tmp_path):
    """英文名逐词词典翻译：iron-spells → 铁魔法？farmer 词表命中。"""
    jar = tmp_path / "farmer-s-delight-mc1.20.1.jar"
    jar.write_bytes(_EMPTY_ZIP)
    # 词典：farmer→农夫、delight→乐事（文件名清洗后逐词翻译）
    assert resolve_mod_name(jar) == "农夫s乐事" or usable_chinese_name(resolve_mod_name(jar))


def test_friendly_output_name_chinese(tmp_path):
    """产物文件名含中文名：{中文名}-简体中文化.jar。"""
    jar = tmp_path / "macaws-windows-mc1.20.1-0.3.5.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("fabric.mod.json",
                    json.dumps({"id": "mcwwindows", "name": "Macaw's Windows"}))
    name = friendly_output_name(jar, "zh_cn")
    assert name == "Macaw 的窗户-简体中文化.jar"
    assert "简体中文化" in name


def test_friendly_output_name_fallback_stem(tmp_path):
    """取不到中文名：回退原 jar stem 加 -{语言}化。"""
    jar = tmp_path / "mymod-mc1.20.1-0.3.5.jar"
    jar.write_bytes(_EMPTY_ZIP)
    assert friendly_output_name(jar, "zh_cn") == "mymod-mc1.20.1-0.3.5-简体中文化.jar"


def test_friendly_output_name_other_lang(tmp_path):
    """后缀随目标语言：zh_tw→繁体中文化、ja_jp→日文化、fr_fr→法文化（不写死汉化）。"""
    jar = tmp_path / "mymod.jar"
    jar.write_bytes(_EMPTY_ZIP)
    assert friendly_output_name(jar, "zh_tw") == "mymod-繁体中文化.jar"
    assert friendly_output_name(jar, "ja_jp") == "mymod-日文化.jar"
    assert friendly_output_name(jar, "fr_fr") == "mymod-法文化.jar"
    # 未知语言代码：回退原样代码
    assert friendly_output_name(jar, "xx_xx") == "mymod-xx_xx化.jar"
