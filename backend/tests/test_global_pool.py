# -*- coding: utf-8 -*-
"""v1.2.8 全局并发池测试：翻译/审查/硬编码判断共享同一 in-flight 信号量。

RPM 预算闸只锁「速率」，锁不住「瞬时并发」——各模块独立满配会叠加
（翻译 14 + 审查 14 同时飞）。本文件验证：
1. LLMClient.translate_batch 用懒创建的全局池（大小=引擎并发）；
2. set_throughput 改并发置空池、下次懒重建为新值；
3. review_translations 复用引擎全局池（不新建独立满配池）；
4. 硬编码判断并发封顶 min(concurrency, 5)。
"""
import asyncio
from types import SimpleNamespace

import pytest
from httpx import AsyncClient, MockTransport, Response

import app.hardcode as hc
import app.review as rv
from app.translate.llm import LLMClient


def _client_with(handler) -> LLMClient:
    client = LLMClient("https://x", "k", "m", concurrency=2, batch_size=10)
    client._client = AsyncClient(transport=MockTransport(handler))
    return client


_OK = Response(200, json={"choices": [{"message": {"content": "[i0] 你好"}}]})


@pytest.mark.asyncio
async def test_translate_batch_uses_shared_conc_sem():
    """v1.2.8：translate_batch 用引擎全局并发池（懒创建），并发大小=引擎并发。"""
    client = _client_with(lambda req: _OK)
    await client.translate_batch(["hello"], "zh_cn")
    assert client._conc_sem is not None
    assert client._conc_sem._value == 2
    await client._client.aclose()


@pytest.mark.asyncio
async def test_translate_batch_chunk_callbacks_fire_per_chunk():
    """v1.2.8：每并发 chunk 请求开始/完成触发 on_chunk_start/on_chunk_done——
    聚合「正在翻译 40 条 × N」的数据源（并发 2、80 条 → 2 chunk 各 40 条）。"""
    starts, dones = [], []

    def handler(req):
        content = "\n".join(f"[i{i}] 译{i}" for i in range(40))
        return Response(200, json={"choices": [{"message": {"content": content}}]})

    client = LLMClient("https://x", "k", "m", concurrency=2, batch_size=40)
    client._client = AsyncClient(transport=MockTransport(handler))
    client.on_chunk_start = lambda n: starts.append(n)
    client.on_chunk_done = lambda n: dones.append(n)
    await client.translate_batch([f"text number {i}" for i in range(80)], "zh_cn")
    assert len(starts) == 2 and set(starts) == {40}   # 2 chunk，各 40 条
    assert len(dones) == 2                             # 都触发 done（计数器归零）
    await client._client.aclose()


@pytest.mark.asyncio
async def test_set_throughput_rebuilds_conc_sem():
    """v1.2.8：set_throughput 改并发 → 置空全局池，下次懒重建为新值。"""
    client = _client_with(lambda req: _OK)
    await client.translate_batch(["hello"], "zh_cn")
    first_sem = client._conc_sem
    assert client.set_throughput(concurrency=5) is True
    assert client._conc_sem is None
    assert client.concurrency == 5
    await client.translate_batch(["hello"], "zh_cn")
    assert client._conc_sem is not None
    assert client._conc_sem is not first_sem
    assert client._conc_sem._value == 5
    # 只改批次不动并发 → 池不重建
    assert client.set_throughput(batch_size=20) is True
    assert client._conc_sem._value == 5
    # 相同值 → 返回 False（无变化）
    assert client.set_throughput(concurrency=5) is False
    await client._client.aclose()


@pytest.mark.asyncio
async def test_review_reuses_engine_global_conc_sem(monkeypatch):
    """v1.2.8：审查复用引擎全局并发池（engine._conc_sem），不新建独立满配池。"""
    sem = asyncio.Semaphore(7)
    engine = SimpleNamespace(
        concurrency=14, model="m", base_url="https://x",
        _conc_sem=sem, _get_client=lambda: None, on_usage=None,
    )

    async def fake_review_batch(engine, client, batch, target_lang, silly_mode=False):
        return {}

    monkeypatch.setattr(rv, "_review_batch", fake_review_batch)
    out = await rv.review_translations(
        engine, [{"key": "k", "source": "s", "translated": "t"}], "zh_cn")
    assert out == []
    assert engine._conc_sem is sem        # 复用同一池，未重建
    assert engine._conc_sem._value == 7   # 池大小不被引擎并发覆盖


@pytest.mark.asyncio
async def test_ai_judge_concurrency_capped_at_5(monkeypatch):
    """v1.2.8：硬编码判断并发封顶 _AI_JUDGE_CONCURRENCY(5)，不无脑满配引擎并发(14)。"""
    peak, cur = {"v": 0}, {"v": 0}
    release = asyncio.Event()

    async def fake_judge(engine, client, batch, target_lang, known_translations=None,
                         silly_mode=False):
        cur["v"] += 1
        peak["v"] = max(peak["v"], cur["v"])
        await release.wait()
        cur["v"] -= 1
        return hc.AiJudgeResult()

    engine = SimpleNamespace(
        concurrency=14, batch_size=40, _get_client=lambda: None)
    monkeypatch.setattr(hc, "_ai_judge_batch", fake_judge)
    cands = [{"text": f"s{i}", "context": []} for i in range(250)]  # 7 批 > 5
    task = asyncio.create_task(hc.ai_judge_translate(engine, cands, "zh_cn"))
    await asyncio.sleep(0.05)   # 等并发展开
    release.set()
    await task
    assert 1 < peak["v"] <= 5    # 有并发、但被 5 封顶
