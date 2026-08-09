# -*- coding: utf-8 -*-
"""M5-1 硬编码字节码扫描与替换核心的测试（真实 javac 编译验证）。"""

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from app.hardcode import (
    is_hardcode_translatable,
    replace_hardcoded_strings,
    scan_hardcoded_strings,
)

JAVA_SRC = '''
public class HelloMod {
    public static void main(String[] args) {
        System.out.println("Hello World");
        System.out.println("Welcome to the server");
        System.out.println("iron_ingot");
        System.out.println("OK");
        System.out.println("mymod:item");
        System.out.println("com.example.Mod");
    }
}
'''


def _make_test_jar(tmp_path: Path) -> Path:
    """javac 编译测试类并打包成 jar。无 javac 则跳过测试。"""
    if shutil.which("javac") is None:
        pytest.skip("无 javac，跳过真实编译测试")
    srcdir = tmp_path / "src"
    srcdir.mkdir()
    (srcdir / "HelloMod.java").write_text(JAVA_SRC, encoding="utf-8")
    classes = tmp_path / "classes"
    classes.mkdir()
    subprocess.run(
        ["javac", "-d", str(classes), str(srcdir / "HelloMod.java")], check=True
    )
    jar = tmp_path / "mod.jar"
    with zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in classes.rglob("*.class"):
            zf.write(f, f.relative_to(classes).as_posix())
    return jar


def test_is_hardcode_translatable():
    assert is_hardcode_translatable("Hello World")
    assert is_hardcode_translatable("欢迎来到服务器")
    assert not is_hardcode_translatable("iron_ingot")      # 技术串（下划线）
    assert not is_hardcode_translatable("OK")               # 纯大写缩写
    assert not is_hardcode_translatable("mymod:item")       # modid 前缀
    assert not is_hardcode_translatable("com.example.Mod")  # 类路径
    assert not is_hardcode_translatable("12345")            # 纯数字


def test_scan_hardcoded_strings(tmp_path):
    jar = _make_test_jar(tmp_path)
    found = scan_hardcoded_strings(jar)
    assert "Hello World" in found
    assert "Welcome to the server" in found
    assert "iron_ingot" not in found
    assert "OK" not in found
    assert "mymod:item" not in found
    assert "com.example.Mod" not in found


def test_replace_hardcoded_strings(tmp_path):
    jar = _make_test_jar(tmp_path)
    mapping = {"Hello World": "你好世界", "Welcome to the server": "欢迎来到服务器"}
    result = replace_hardcoded_strings(jar, mapping)
    assert result["replaced"] == 2
    assert result["failed_classes"] == []
    # 重读验证：jar 内字符串已被替换
    found = scan_hardcoded_strings(jar)
    assert "你好世界" in found
    assert "欢迎来到服务器" in found
    assert "Hello World" not in found
