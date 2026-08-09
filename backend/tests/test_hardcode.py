# -*- coding: utf-8 -*-
"""M5-1 硬编码字节码扫描与替换核心的测试（真实 javac 编译验证）。"""

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

import app.hardcode as hardcode
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


# ---------- 嵌套包类用例（com.example 包路径斜杠规范化覆盖） ----------

NESTED_JAVA_SRC = '''
package com.example;

public class NestedMod {
    public static void main(String[] args) {
        System.out.println("Nested Hello");
        System.out.println("Nested Welcome");
    }
}
'''


def _make_nested_jar(tmp_path: Path) -> Path:
    """javac 编译带 package 声明的嵌套类并打包成 jar。无 javac 则跳过。"""
    if shutil.which("javac") is None:
        pytest.skip("无 javac，跳过真实编译测试")
    srcdir = tmp_path / "src" / "com" / "example"
    srcdir.mkdir(parents=True)
    (srcdir / "NestedMod.java").write_text(NESTED_JAVA_SRC, encoding="utf-8")
    classes = tmp_path / "classes"
    classes.mkdir()
    subprocess.run(
        ["javac", "-d", str(classes), str(srcdir / "NestedMod.java")], check=True
    )
    # 编译产物应在 com/example/NestedMod.class
    nested = classes / "com" / "example" / "NestedMod.class"
    assert nested.is_file(), "javac 未产出嵌套类 class 文件"
    jar = tmp_path / "nested.jar"
    with zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in classes.rglob("*.class"):
            zf.write(f, f.relative_to(classes).as_posix())
    return jar


def test_scan_nested_package_class(tmp_path):
    jar = _make_nested_jar(tmp_path)
    found = scan_hardcoded_strings(jar)
    assert "Nested Hello" in found
    assert "Nested Welcome" in found


def test_replace_nested_package_class(tmp_path):
    jar = _make_nested_jar(tmp_path)
    mapping = {"Nested Hello": "嵌套你好", "Nested Welcome": "嵌套欢迎"}
    result = replace_hardcoded_strings(jar, mapping)
    assert result["replaced"] == 2
    assert result["failed_classes"] == []
    # 重读验证：嵌套类内字符串已被替换
    found = scan_hardcoded_strings(jar)
    assert "嵌套你好" in found
    assert "嵌套欢迎" in found
    assert "Nested Hello" not in found
    assert "Nested Welcome" not in found


# ---------- 损坏 class 容错（scan 不因单 class 崩溃） ----------

def test_scan_skip_bad_class(tmp_path):
    jar = _make_test_jar(tmp_path)
    evil = tmp_path / "bad.jar"
    with zipfile.ZipFile(evil, "w", zipfile.ZIP_DEFLATED) as zf:
        with zipfile.ZipFile(jar, "r") as src:
            for item in src.infolist():
                zf.writestr(item, src.read(item.filename))
        # 注入一个无法被 jawa 解析的损坏 class
        zf.writestr("Broken.class", "not a real class file")
    # 不应抛异常：坏 class 被跳过，正常 class 字符串仍能扫出
    found = scan_hardcoded_strings(evil)
    assert "Hello World" in found
    assert "Welcome to the server" in found


# ---------- zip-slip 路径穿越防护 ----------

def test_extract_jar_zip_slip(tmp_path):
    """恶意 jar 含 ../ 条目时不得逃逸出解压目录。"""
    jar = _make_test_jar(tmp_path)
    evil = tmp_path / "evil.jar"
    with zipfile.ZipFile(evil, "w", zipfile.ZIP_DEFLATED) as zf:
        with zipfile.ZipFile(jar, "r") as src:
            for item in src.infolist():
                zf.writestr(item, src.read(item.filename))
        zf.writestr("../escaped.txt", "PAYLOAD")
    # 扫描内部会解压：逃逸条目必须被跳过，不能落到 tmp_path
    scan_hardcoded_strings(evil)
    assert not (tmp_path / "escaped.txt").exists()
    # 替换同样不能逃逸
    replace_hardcoded_strings(evil, {"Hello World": "你好世界"})
    assert not (tmp_path / "escaped.txt").exists()


# ---------- replace 校验失败时原始字节还原 ----------

def test_replace_restores_bytes_on_verify_failure(tmp_path, monkeypatch):
    """校验失败（模拟重读抛异常）时 class 必须还原为原字节，不进输出 jar。"""
    jar = _make_test_jar(tmp_path)
    real_loader = hardcode._class_loader
    calls = {"n": 0}

    def flaky_loader(work):
        calls["n"] += 1
        if calls["n"] >= 2:  # 第二次调用即校验阶段：模拟重读失败
            raise RuntimeError("模拟校验失败")
        return real_loader(work)

    monkeypatch.setattr(hardcode, "_class_loader", flaky_loader)
    result = replace_hardcoded_strings(jar, {"Hello World": "你好世界"})
    monkeypatch.undo()
    # 失败 class 被记录，且替换数必须为 0（还原后无效替换）
    assert result["failed_classes"], "应记录校验失败的 class"
    assert result["replaced"] == 0
    # 输出 jar 重读：原字符串保留，被替换的字符串不得出现
    found = scan_hardcoded_strings(jar)
    assert "Hello World" in found
    assert "你好世界" not in found
