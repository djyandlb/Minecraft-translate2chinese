from app.translate.providers import PROVIDERS, smart_defaults


def test_providers_have_expected_keys():
    for name in ("DeepSeek", "通义千问", "Kimi", "Ollama", "自定义"):
        assert name in PROVIDERS
        assert {"base_url", "model", "concurrency", "batch_size"} <= set(PROVIDERS[name])


def test_smart_defaults_known_provider():
    d = smart_defaults("DeepSeek")
    assert d["model"] == "deepseek-chat" and d["concurrency"] == 8


def test_smart_defaults_unknown_fallback():
    d = smart_defaults("不存在的厂商")
    assert d["base_url"] == "" and d["concurrency"] == 5  # 回退"自定义"
