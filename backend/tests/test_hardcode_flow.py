# -*- coding: utf-8 -*-
"""M5-2 硬编码翻译后台流程的测试（真实 javac 编译验证）。

验证 run_hardcode_translation：复制原 jar → 扫描 → 引擎翻译 → 替换校验 → 输出新 jar。
核心铁律：原 jar 只读（一切写操作只在 work 副本）。
"""

import shutil
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.hardcode import scan_hardcoded_strings
from app.hardcode_flow import run_hardcode_translation
from app.tasks import TaskStore


class _FakeEngine:
    """假翻译引擎：把 Hello World 换成 你好世界，其余串加 [译] 前缀。"""

    def __init__(self, target_lang: str):
        self._target = target_lang

    async def translate_batch(self, texts, target_lang):
        return [
            t.replace("Hello World", "你好世界") if t == "Hello World" else f"[译]{t}"
            for t in texts
        ]


def _make_test_jar(tmp_path: Path) -> Path:
    """javac 编译含硬编码字符串的类并打包（无 javac 则 skip）。"""
    if shutil.which("javac") is None:
        pytest.skip("无 javac")
    srcdir = tmp_path / "src"
    srcdir.mkdir()
    (srcdir / "HelloMod.java").write_text(
        'public class HelloMod { public static void main(String[] a) { System.out.println("Hello World"); } }',
        encoding="utf-8",
    )
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


@pytest.mark.asyncio
async def test_run_hardcode_translation(tmp_path, monkeypatch):
    jar = _make_test_jar(tmp_path)
    # 注入假引擎（cfg 传 None 即可，引擎创建被拦截，不真正访问 cfg）
    monkeypatch.setattr(
        "app.hardcode_flow.create_engine", lambda cfg: _FakeEngine("zh_cn")
    )
    # 任务 + 工作区
    store = TaskStore(tmp_path / "tasks")
    state = store.new()
    state.status = "running"
    store.save(state)
    work = tmp_path / "work"
    work.mkdir()
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    req = SimpleNamespace(path=str(jar), source_lang="en_us", target_lang="zh_cn")
    await run_hardcode_translation(state.id, req, None, store, work, outputs)
    # 铁律：原 jar 未被改写（原档只读）
    assert scan_hardcoded_strings(jar) == ["Hello World"]
    # 输出 jar 存在且已汉化
    outs = list(outputs.glob("*_hardcoded.jar"))
    assert outs, "输出 jar 未生成"
    found = scan_hardcoded_strings(outs[0])
    assert "你好世界" in found
    assert "Hello World" not in found
    # 任务状态 done
    final = store.load(state.id)
    assert final.status == "done"
