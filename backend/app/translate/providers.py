# V3 决策：LLM 厂商预置模板。base_url/model + 智能并发/批大小默认值（"自动设置"的落地）。
# 用户可在 UI 覆盖这些值；未知厂商由 smart_defaults 回退"自定义"。
PROVIDERS: dict[str, dict] = {
    "DeepSeek":   {"base_url": "https://api.deepseek.com", "model": "deepseek-chat",
                   "concurrency": 8, "batch_size": 20},
    "通义千问":     {"base_url": "https://dashscope.aliyuncs.com/compatible-mode",
                   "model": "qwen-plus", "concurrency": 6, "batch_size": 20},
    "Kimi":       {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k",
                   "concurrency": 5, "batch_size": 20},
    "Ollama":     {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:7b",
                   "concurrency": 2, "batch_size": 10},
    "自定义":       {"base_url": "", "model": "", "concurrency": 5, "batch_size": 20},
}


def smart_defaults(provider: str) -> dict:
    """返回该厂商智能默认并发/批大小；未知厂商回退"自定义"。"""
    return PROVIDERS.get(provider, PROVIDERS["自定义"])
