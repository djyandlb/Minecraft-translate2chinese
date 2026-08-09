import json

import pytest
from httpx import AsyncClient, MockTransport, Response
from app.translate.llm import LLMClient, build_tagged_texts, parse_tagged, clean_translation

def test_tagged_roundtrip():
    prompt = build_tagged_texts(["a", "b", "c"])
    assert prompt == "[i0] a\n[i1] b\n[i2] c"
    parsed = parse_tagged("[i0] 甲\n[i1] 乙\n[i2] 丙")
    assert parsed == {0: "甲", 1: "乙", 2: "丙"}

def test_clean_translation():
    assert clean_translation("```\n翻译：铁锭\n```") == "铁锭"
    assert clean_translation('"铁块"') == "铁块"
    assert clean_translation("结果: 金锭") == "金锭"

def _client_with(handler) -> LLMClient:
    client = LLMClient("https://x", "k", "m", concurrency=2, batch_size=10)
    client._client = AsyncClient(transport=MockTransport(handler))
    return client

@pytest.mark.asyncio
async def test_translate_batch_tagged():
    def handler(request):
        return Response(200, json={"choices": [{"message": {"content": "[i0] 你好\n[i1] 再见"}}]})
    client = _client_with(handler)
    out = await client.translate_batch(["hello", "world"], "zh_cn")
    assert out == ["你好", "再见"]
    await client._client.aclose()

@pytest.mark.asyncio
async def test_technical_string_unchanged():
    def handler(request):
        return Response(200, json={"choices": [{"message": {"content": "[i0] 译"}}]})
    client = _client_with(handler)
    out = await client.translate_batch(["iron_ingot", "abc:def"], "zh_cn")
    assert out == ["iron_ingot", "abc:def"]   # 技术串跳过，不调 API
    await client._client.aclose()

@pytest.mark.asyncio
async def test_usage_callback():
    usages = []
    def handler(request):
        return Response(200, json={"choices": [{"message": {"content": "[i0] 你好"}}],
                                   "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
    client = LLMClient("https://x", "k", "m", on_usage=lambda a, b: usages.append((a, b)))
    client._client = AsyncClient(transport=MockTransport(handler))
    await client.translate_batch(["hello"], "zh_cn")
    assert usages == [(10, 5)]
    await client._client.aclose()

@pytest.mark.asyncio
async def test_http_failure_falls_back_to_original():
    def handler(request):
        return Response(500)
    client = _client_with(handler)
    out = await client.translate_batch(["hello world"], "zh_cn")
    assert out == ["hello world"]   # 请求失败 → 回原文
    await client._client.aclose()

@pytest.mark.asyncio
async def test_downgrade_half_split_retries():
    # 首次请求 500 → 触发对半切 → 逐条重试成功
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return Response(500)
        # 对半切后子块仍走 _request_chunk 拼 prompt（形如 "[i0] hello"），
        # 需按内容识别子块并返回带 [iN] 前缀的输出才能被 parse_tagged 解析
        content = json.loads(request.content)["messages"][-1]["content"]
        if "hello" in content:
            return Response(200, json={"choices": [{"message": {"content": "[i0] 你好"}}]})
        return Response(200, json={"choices": [{"message": {"content": "[i0] 再见"}}]})

    client = _client_with(handler)
    out = await client.translate_batch(["hello", "world"], "zh_cn")
    assert out == ["你好", "再见"]
    assert calls["n"] >= 2   # 至少发生了降级重试
    await client._client.aclose()


@pytest.mark.asyncio
async def test_full_failure_returns_original():
    # 全链失败 → 全部回原文
    def handler(request):
        return Response(500)

    client = _client_with(handler)
    out = await client.translate_batch(["hello world", "good day"], "zh_cn")
    assert out == ["hello world", "good day"]
    await client._client.aclose()
