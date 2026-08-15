# -*- coding: utf-8 -*-
"""recheck 全量修复回归测试（v1.2.8）：
1. 非拉丁脚本（西里尔/谚文/假名/希腊/泰文）不再被 lang_value_ok/should_translate 滤掉漏翻
2. verify 目标语言 json 带 BOM 不再误判「语言文件损坏」
3. MachineClient.set_throughput 并发 cap 5（不因热更新绕过）
4. scanner 单个坏语言文件不再丢弃整个 jar 的其他 mod
5. RateGate(rpm=0, auto=False) 连续 acquire 不除零
"""
import asyncio
import json
import zipfile

import pytest

from app.langfile import lang_value_ok
from app.translate.common import should_translate
from app.translate.machine import MachineClient
from app.translate.ratelimit import RateGate


def test_lang_value_ok_non_latin_scripts():
    """v1.2.8 修复：西里尔/谚文/希腊/泰文值不被滤掉（非拉丁源不漏翻）。"""
    assert lang_value_ok("Привет мир")          # 俄文
    assert lang_value_ok("안녕하세요")            # 韩文
    assert lang_value_ok("Γειά σου")            # 希腊
    assert lang_value_ok("สวัสดี")               # 泰文
    assert lang_value_ok("こんにちは")            # 日文假名
    assert not lang_value_ok("12345")            # 纯数字仍滤掉
    assert not lang_value_ok("---")              # 纯符号仍滤掉


def test_should_translate_non_latin_scripts():
    """v1.2.8 修复：非拉丁脚本文本进入翻译（json/lines/硬编码阶段不漏翻）。"""
    assert should_translate("Привет мир")
    assert should_translate("안녕하세요 세상")
    assert should_translate("こんにちは世界")
    assert not should_translate("12345")


def test_verify_json_with_bom(tmp_path):
    """v1.2.8 修复：目标语言 json 带 BOM 不再误判「语言文件损坏」导致整 jar 不输出。"""
    from app.verify import verify_translated_jar
    jar = tmp_path / "m.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("assets/demo/lang/zh_cn.json",
                    b"\xef\xbb\xbf" + json.dumps({"key": "值"}).encode("utf-8"))
    r = verify_translated_jar(jar, "zh_cn")
    assert r["ok"] is True, r


def test_machine_set_throughput_caps_at_5():
    """v1.2.8 修复：MachineClient.set_throughput 并发封顶 5（Google 免费限流）。"""
    m = MachineClient(provider="google", concurrency=3)
    assert m.concurrency == 3
    m.set_throughput(concurrency=99)
    assert m.concurrency == 5
    m.set_throughput(concurrency=2)
    assert m.concurrency == 2


@pytest.mark.asyncio
async def test_rate_gate_zero_auto_false_no_divzero():
    """v1.2.8 修复：RateGate(rpm=0, auto=False) 超配额 acquire 不除零。"""
    g = RateGate(0, auto=False)
    await g.acquire()   # 第一次：桶满通过
    # 第二次：桶空，走到 sleep((1-tokens)/rate)——rate 已被兜底 >0，不会除零
    try:
        await asyncio.wait_for(g.acquire(), timeout=0.05)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass


def test_scanner_single_bad_lang_file_keeps_other_mods(tmp_path):
    """v1.2.8 修复：jar 内单个坏语言文件只跳过该 mod，不再丢弃整个 jar。"""
    from app.scanner import scan_jar
    jar = tmp_path / "bad.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("assets/a/lang/en_us.json", json.dumps({"ok": "fine"}))
        zf.writestr("assets/b/lang/en_us.json", "{broken: trailing,}")  # 坏 json
    results = scan_jar(jar, "en_us", "zh_cn")
    assert len(results) == 1      # mod a 保留，mod b 跳过
    assert results[0].modid == "a"
