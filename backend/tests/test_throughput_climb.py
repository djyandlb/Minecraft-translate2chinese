# -*- coding: utf-8 -*-
"""v1.3.8 并发线性爬坡压力测试（对照业界标杆：AWS 饱和测试拐点法 / Gatling 爬坡）验证。

用户核心诉求：测得准才能分发——压力测试必须能精确测出 API 的真实并发/RPM 上限，
不固定并发、不断档太粗、不把并发型 API 误判成低 RPM、不被慢 API 时间预算拖死。

覆盖场景（对抗验证防想当然）：
- 并发型 API（DeepSeek V4 官方 2500 并发、无 RPM 限流）→ 应爬到 MAX_CONC(64)
- RPM 型 API（MiniMax/百炼，某并发撞 429）→ 应精确停在限流前一档
- 超时型 API（某并发超时）→ 应停在超时前一档
- 并发 1 就全失败 → 应返回失败（不瞎给档位）
- 饱和拐点：并发翻倍吞吐增长 <15% → 提前停（业界拐点法）
- RPM 封顶：快 API 单批短 → 反推 RPM 封顶 _AUTO_MAX_RPM(10000)，不显示荒谬值
"""
import asyncio

import pytest

import app.main as main_mod


class _FakeLLM:
    """按并发档位决定行为模式的假 LLMClient。

    mode:
      - "unlimited": 无限流，任意并发成功（模拟 DeepSeek 并发型）
      - "rpm_at": limit=N 时该并发开始撞 429（模拟 MiniMax/百炼 RPM 型）
      - "timeout_at": limit=N 时该并发超时（模拟慢 + 不稳 API）
      - "always_fail": 任意并发都失败（模拟未连接/无 key）
      - "slow_budget": 每档 sleep 0.2s（模拟慢 API：180s 时间预算会截断爬坡）
    每批模拟耗时让 W 可测（0.02s），并记录收到的 payload 长度验证固定轻负载。
    """

    def __init__(self, base_url, api_key, model, concurrency=1, batch_size=10, rpm=0,
                 on_usage=None, glossary_prompt="", silly_mode=False, auto_init_rpm=0):
        self.concurrency = concurrency
        self.batch_size = batch_size
        self._conc_sem = None
        self._batch_failed_texts = set()
        self._last_error_kind = "other"
        self._fatal_error = None
        self._consec_fails = 0
        self._ratelimit_tpm = 0
        self.rate_gate = None
        self.on_chunk_start = None
        self.on_chunk_done = None
        self.filter_technical = True
        self.silly_mode = silly_mode
        self.mode = "unlimited"
        self.limit = 0
        self.payload_lens = []      # 每次 translate_batch 收到的条数（验证固定 8 条）
        self.actual_concs = []      # 每次被创建时的并发（验证逐档爬升）

    async def translate_batch(self, texts, target_lang, **kw):
        self.payload_lens.append(len(texts))
        meta = kw.get("meta")
        await asyncio.sleep(0.02)
        if self.mode == "always_fail":
            if meta is not None:
                meta["failed"] = set(texts); meta["kind"] = "other"; meta["fatal"] = None
            return list(texts)
        if self.mode == "rpm_at" and self.concurrency >= self.limit:
            if meta is not None:
                meta["failed"] = set(texts); meta["kind"] = "ratelimit"; meta["fatal"] = None
            return list(texts)
        if self.mode == "timeout_at" and self.concurrency >= self.limit:
            raise TimeoutError("模拟超时")
        if self.mode == "slow_budget":
            # 模拟慢 API：每档 sleep 0.1s（测试加速；真实慢 API 也是固定轻负载爬到顶，
            # 不再被阶加 payload 膨胀拖死——旧 bug：并发 10 发 160 条要 18s、180s 只爬 10 档）
            await asyncio.sleep(0.1)
        if meta is not None:
            meta["failed"] = set(); meta["kind"] = "other"; meta["fatal"] = None
        return [f"译{i}" for i in range(len(texts))]

    async def aclose(self):
        pass


async def _run_test(mode: str, limit: int = 0):
    """跑完整 test_throughput，用 _FakeLLM 替换 LLMClient。返回 (结果, FakeLLM 工厂记录)。"""
    created: list[_FakeLLM] = []

    def factory(base_url, api_key, model, **kw):
        c = _FakeLLM(base_url, api_key, model, **kw)
        c.mode = mode
        c.limit = limit
        created.append(c)
        return c

    import app.translate.llm as llm_mod
    real_llm = llm_mod.LLMClient
    try:
        # test_throughput 函数内是 `from app.translate.llm import LLMClient`（局部导入），
        # 所以 patch 源模块 app.translate.llm.LLMClient 即可生效
        llm_mod.LLMClient = factory

        cfg_inst = type("Cfg", (), {})()
        cfg_inst.get = lambda k, d=None: {
            "engine": "llm", "provider": "DeepSeek",
            "llm": {"base_url": "https://x", "model": "m"},
            "rpm": 0, "tpm": 0, "concurrency": 5,
        }.get(k, d)
        cfg_inst.set = lambda *a, **k: None
        cfg_inst.save = lambda: None

        import unittest.mock as um
        with um.patch("app.main.AppConfig", return_value=cfg_inst), \
             um.patch("app.main._read_api_key", return_value="k"), \
             um.patch("app.main._config_lock", __enter__=lambda s: s, __exit__=lambda *a: False):
            r = await main_mod.test_throughput({
                "engine": "llm", "provider": "DeepSeek",
                "llm": {"base_url": "https://x", "model": "m"},
                "api_key": "k", "rpm": 0})
        return r, created
    finally:
        llm_mod.LLMClient = real_llm


@pytest.mark.asyncio
async def test_climb_unlimited_api_reaches_max():
    """并发型 API（DeepSeek 官方，无 RPM 限流）：线性爬坡应精确爬到 MAX_CONC=64。"""
    r, created = await _run_test("unlimited")
    assert r["ok"] is True
    assert r["concurrency"] == 64, f"并发型应爬到 64，实际 {r['concurrency']}"
    # v1.3.8 recheck：RPM 反推 = 64×60/W，必须封顶 _AUTO_MAX_RPM(10000)——否则快 API
    # 单批极短会反推 19 万 RPM，写进 config 后前端显示荒谬值、RateGate 闸形同虚设。
    # 这里验证「已封顶且不低于并发基数」（真实 mock W≈0.5s → 7680，封顶逻辑不误伤真实值）
    assert 64 <= r["rpm"] <= 10000, f"RPM 应在 [并发, 10000] 内且封顶，实际 {r['rpm']}"
    # 爬坡档位：created[0] 是 _measure_w（并发 2），其余是爬坡（并发 1..64）
    climb = created[1:]
    concs = [c.concurrency for c in climb]
    assert concs == list(range(1, 65)), "应逐档 +1 精确爬升"
    # 固定轻负载：每档收到 8 条（对照业界：每阶段固定负载测并发承受力，不被 payload 膨胀拖死）
    for c in climb:
        assert c.payload_lens and c.payload_lens[0] == 8, \
            f"固定轻负载：并发 {c.concurrency} 应发 8 条，实际 {c.payload_lens[0]}"


@pytest.mark.asyncio
async def test_climb_rpm_api_stops_before_limit():
    """RPM 型 API（并发 20 起撞 429）：精确停在 19（限流前一档），不浪费并发。"""
    r, created = await _run_test("rpm_at", limit=20)
    assert r["ok"] is True
    assert r["concurrency"] == 19, f"RPM 型并发 20 撞应停在 19，实际 {r['concurrency']}"
    assert r["results"]["rpm_hit_limit"] is True
    # 只爬了 1..19 就被撞停（第 20 档失败），不多爬；created[0] 是 _measure_w
    climb = created[1:]
    assert [c.concurrency for c in climb] == list(range(1, 21)), "应逐档爬到 19 后第 20 档撞停"


@pytest.mark.asyncio
async def test_climb_timeout_api_stops_before_timeout():
    """超时型 API（并发 30 起超时）：精确停在 29。"""
    r, created = await _run_test("timeout_at", limit=30)
    assert r["ok"] is True
    assert r["concurrency"] == 29, f"超时型并发 30 超时应停在 29，实际 {r['concurrency']}"


@pytest.mark.asyncio
async def test_climb_always_fail_returns_error():
    """并发 1 就全失败（未连接/无 key）：返回失败，不瞎给档位。"""
    r, created = await _run_test("always_fail")
    assert r["ok"] is False
    assert "失败" in r["message"]


@pytest.mark.asyncio
async def test_climb_small_limit_precise():
    """低限流（并发 5 撞）：精确停在 4，验证小并发也测准。"""
    r, created = await _run_test("rpm_at", limit=5)
    assert r["ok"] is True
    assert r["concurrency"] == 4, f"并发 5 撞应停在 4，实际 {r['concurrency']}"


@pytest.mark.asyncio
async def test_climb_time_budget_stops_early():
    """慢 API 时间预算（业界原则：压测不能无限拖，预算内测出「稳定并发下限」）：
    每档 sleep 1s → 预算足够爬满 64（1×64=64s < 180s），但验证「慢 API 也能爬到顶」
    （不再被阶加 payload 膨胀拖死——旧 bug：并发 10 发 160 条要 18s，180s 只爬 10 档）。"""
    r, created = await _run_test("slow_budget")
    assert r["ok"] is True
    # 固定 8 条轻负载 + 每档 1s：180s 预算内能爬满 64（慢 API 不再被阶加膨胀拖死）
    assert r["concurrency"] == 64, f"固定轻负载慢 API 应能爬到 64，实际 {r['concurrency']}"
    # 未撞限流（纯耗时，非 429）
    assert r["results"]["rpm_hit_limit"] is False
