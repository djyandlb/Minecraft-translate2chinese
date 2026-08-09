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


@pytest.mark.asyncio
async def test_glossary_prompt_injected():
    # 术语表注入（任务 13）：glossary_prompt 非空时拼到 system 提示词最前
    captured = {}

    def handler(request):
        captured["system"] = json.loads(request.content)["messages"][0]["content"]
        return Response(200, json={"choices": [{"message": {"content": "[i0] 铁锭"}}]})

    client = LLMClient("https://x", "k", "m",
                       glossary_prompt="术语表（翻译必须遵守）：\niron_ingot => 铁锭")
    client._client = AsyncClient(transport=MockTransport(handler))
    await client.translate_batch(["iron ingot"], "zh_cn")
    assert captured["system"].startswith("术语表（翻译必须遵守）：")
    assert "iron_ingot => 铁锭" in captured["system"]
    assert "把 Minecraft" in captured["system"]
    await client._client.aclose()


# ---------- P0 止血：单条/批量翻译丢失容错 ----------

@pytest.mark.asyncio
async def test_translate_batch_single_no_tag():
    """单条翻译：模型输出无 [iN] 标签的纯译文 → 直接采用，不得回原文（P0 根因 1）。"""
    def handler(request):
        return Response(200, json={"choices": [{"message": {"content": "你好世界"}}]})

    client = _client_with(handler)
    out = await client.translate_batch(["Hello World"], "zh_cn")
    assert out == ["你好世界"]
    await client._client.aclose()


@pytest.mark.asyncio
async def test_translate_batch_single_tagged_also_ok():
    """单条翻译：模型仍输出 [i0] 前缀（旧行为）→ 剥标签后采用。"""
    def handler(request):
        return Response(200, json={"choices": [{"message": {"content": "[i0] 你好世界"}}]})

    client = _client_with(handler)
    out = await client.translate_batch(["Hello World"], "zh_cn")
    assert out == ["你好世界"]
    await client._client.aclose()


@pytest.mark.asyncio
async def test_translate_batch_single_placeholder_restored():
    """单条翻译带占位符：无标签译文里的 %%MC_0%% 要还原回 %s。"""
    def handler(request):
        return Response(200, json={"choices": [{"message": {"content": "你好 %s"}}]})

    client = _client_with(handler)
    out = await client.translate_batch(["Hello %s"], "zh_cn")
    assert out == ["你好 %s"]
    await client._client.aclose()


@pytest.mark.asyncio
async def test_translate_batch_missing_tag_downgrades():
    """批量缺标：模型只输出 [i0] 缺 i1 → i1 逐条降级 _translate_single，不静默保留原文（P0 根因 2）。"""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return Response(200, json={"choices": [{"message": {"content": "[i0] 你好"}}]})
        # 第二次请求是 i1 的逐条降级：单条 prompt，无标签直接出译文
        return Response(200, json={"choices": [{"message": {"content": "再见"}}]})

    client = _client_with(handler)
    out = await client.translate_batch(["hello", "world"], "zh_cn")
    assert out == ["你好", "再见"]
    assert calls["n"] == 2   # 一次批量 + 一次逐条降级
    await client._client.aclose()


@pytest.mark.asyncio
async def test_translate_batch_bad_format_half_split():
    """整批格式不符（无任何标签）→ 对半切批重试，逐条降级成功（P0 根因 2）。"""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return Response(200, json={"choices": [{"message": {"content": "整体一句话"}}]})
        # 对半切后单条请求：按内容返回对应译文
        content = json.loads(request.content)["messages"][-1]["content"]
        if "hello" in content:
            return Response(200, json={"choices": [{"message": {"content": "你好"}}]})
        return Response(200, json={"choices": [{"message": {"content": "再见"}}]})

    client = _client_with(handler)
    out = await client.translate_batch(["hello", "world"], "zh_cn")
    assert out == ["你好", "再见"]
    assert calls["n"] == 3   # 1 批量 + 2 单条降级
    await client._client.aclose()


def test_parse_tagged_numeric_and_note():
    """parse_tagged 容错：数字. 前缀映射索引 + 忽略末尾「以上是全部译文」说明行。"""
    parsed = parse_tagged("1. 甲\n2. 乙\n以上是全部译文")
    assert parsed == {0: "甲", 1: "乙"}


def test_parse_tagged_mixed_prefixes():
    """parse_tagged 容错：混合 [iN] 与 **iN** 前缀。"""
    parsed = parse_tagged("**i0** 甲\n[i1] 乙")
    assert parsed == {0: "甲", 1: "乙"}


def test_parse_tagged_inline_split():
    """parse_tagged 容错：合并行 '[i0] 甲 [i1] 乙' 拆成两条。"""
    parsed = parse_tagged("[i0] 甲 [i1] 乙")
    assert parsed == {0: "甲", 1: "乙"}
