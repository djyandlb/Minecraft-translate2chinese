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


def test_mixed_long_english_caught_by_review(tmp_path):
    """v1.3.0（用户「大段英文连着还判过」）：大段英文 + 零星中文——_is_target_lang 含 1
    汉字判「目标语言」，但 _has_english_leak 判残留 → ok_items 初审补的拦截会把它送进重翻。"""
    from types import SimpleNamespace
    from app.auto_flow import AutoFlow
    from app.tasks import TaskState, TaskStore
    store = TaskStore(tmp_path / "tasks")
    store.save(TaskState(id="t1"))
    req = SimpleNamespace(target_lang="zh_cn", source_lang="en_us", path=str(tmp_path / "pack.zip"))
    flow = AutoFlow("t1", req, None, store, tmp_path / "work", tmp_path / "out", None)
    mixed = "This is a very long English description about how to use this item in your base. 使用这个物品"
    assert flow._is_target_lang(mixed, "zh_cn")   # 含汉字 → 目标语言
    assert flow._has_english_leak(mixed)          # 但大段英文 → 残留（拦截依据）
    # 纯中文无残留
    assert not flow._has_english_leak("这是一段完全中文的翻译内容，介绍如何使用这个物品。")


def test_json_should_translate_skips_switch_literals():
    """v1.2.9/1.3.0：jar 内 json 的纯布尔 true/false 不翻译（用户实测
    components[3].link_recipe 的 "true" 被翻成「是」）；
    v1.3.0 修复：enabled/on/none/yes 等真实按钮文本**恢复翻译**（原 _SWITCH_LITERALS 误杀
    「Enabled」→ 保留英文，用户「全英文」根因之一）。"""
    from app.text_sources import _json_should_translate
    assert not _json_should_translate("true")
    assert not _json_should_translate("false")
    # 真实文本按钮恢复翻译（v1.3.0 修复误杀）
    assert _json_should_translate("enabled")
    assert _json_should_translate("on")
    assert _json_should_translate("none")
    assert _json_should_translate("yes")
    # 真实文本载体仍正常提取
    assert _json_should_translate("This is a real recipe description")


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


@pytest.mark.asyncio
async def test_pipeline_flush_threshold_scales_with_concurrency(tmp_path, monkeypatch):
    """v1.2.8 修复：flush 阈值 = batch_size×并发 → 一次 translate_batch 收到多 chunk 总量
    （并发真正施展）。原阈值=batch_size(40) → 每次只发 1 个请求（用户实测「一批一批上传」）。"""
    from types import SimpleNamespace
    from app.auto_flow import AutoFlow
    from app.tasks import TaskState, TaskStore

    store = TaskStore(tmp_path / "tasks")
    store.save(TaskState(id="t1"))
    req = SimpleNamespace(target_lang="zh_cn", source_lang="en_us",
                          path=str(tmp_path / "pack.zip"))
    flow = AutoFlow("t1", req, None, store, tmp_path / "work", tmp_path / "out", None)
    flow.engine = SimpleNamespace(concurrency=4, batch_size=40)   # 4×40=160 阈值
    flow.same_script = False
    received: list[int] = []

    async def fake_translate(texts, reasons=None):
        received.append(len(texts))
        return [f"译{i}" for i in range(len(texts))], {}

    items = [{"key": f"k{i}", "text": f"Item number {i} description here", "sink": {}}
             for i in range(200)]
    await flow._translate_batch_pipeline(items, fake_translate, batch_size=40)
    # 200 条 → 首 flush 160（=40×4 并发阈值，translate_batch 内部切 4 chunk 并发），
    # 收尾 flush 剩余 40。原逻辑会是 [40,40,40,40,40]（一次只 1 请求）。
    assert received == [160, 40], received


@pytest.mark.asyncio
async def test_pipeline_flush_threshold_single_retranslation_keeps_1(tmp_path):
    """v1.2.8：batch_size=1 的漏翻逐条重翻不放大阈值（保持逐条专注语义）。"""
    from types import SimpleNamespace
    from app.auto_flow import AutoFlow
    from app.tasks import TaskState, TaskStore

    store = TaskStore(tmp_path / "tasks")
    store.save(TaskState(id="t1"))
    req = SimpleNamespace(target_lang="zh_cn", source_lang="en_us",
                          path=str(tmp_path / "pack.zip"))
    flow = AutoFlow("t1", req, None, store, tmp_path / "work", tmp_path / "out", None)
    flow.engine = SimpleNamespace(concurrency=4, batch_size=40)
    flow.same_script = False
    received: list[int] = []

    async def fake_translate(texts, reasons=None):
        received.append(len(texts))
        return [f"译{i}" for i in range(len(texts))], {}

    items = [{"key": f"k{i}", "text": f"Lonely leak {i}", "sink": {}} for i in range(3)]
    await flow._translate_batch_pipeline(items, fake_translate, batch_size=1)
    assert received == [1, 1, 1], received   # 逐条，不攒 4 条


@pytest.mark.asyncio
async def test_review_chunk_callback_pushes_aggregate(tmp_path):
    """v1.2.9：审查批开始/完成推聚合「静默审查中 N 条 × M」——审查也并发（40×16）的可视化。"""
    from types import SimpleNamespace
    from app.auto_flow import AutoFlow
    from app.tasks import TaskState, TaskStore

    store = TaskStore(tmp_path / "tasks")
    store.save(TaskState(id="t1"))
    req = SimpleNamespace(target_lang="zh_cn", source_lang="en_us",
                          path=str(tmp_path / "pack.zip"))
    flow = AutoFlow("t1", req, None, store, tmp_path / "work", tmp_path / "out", None)
    flow._review_chunk_start_cb(40)
    flow._review_chunk_start_cb(40)
    flow._review_chunk_start_cb(40)
    assert flow._active_review == 3
    flow._review_chunk_done_cb(40)
    assert flow._active_review == 2
    # v1.2.9：审查不再 push 聚合提示（用户诉求：去掉「静默审查中」计数条，
    # done 计数改由审查写回 _write_reviewed 推进，读数即审查进度）
    acts = [p for p in flow.state.progress if p.get("key") == "@active_review"]
    assert acts == []
