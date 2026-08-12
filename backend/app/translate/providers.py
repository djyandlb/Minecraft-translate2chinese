# V3 决策：LLM 厂商预置模板。base_url/model + 智能并发/批大小默认值（"自动设置"的落地）。
# 用户可在 UI 覆盖这些值；未知厂商由 smart_defaults 回退"自定义"。
#
# batch_size（吞吐核心）：一次请求翻 N 条。默认 25（用户反馈 10 条一批太慢，37025 条要 3700
# 个请求）。25 条/批 + 并发 5-8 = 同时 150-200 条进行，吞吐提升 2.5 倍。长文本/限流平台
# （Ollama/讯飞/免费）用小批 12 防超时/限速；120s 超时 + max_tokens 8192 + 降级链兜底。
# 设置页「批量大小」可调（5-60，默认厂商）。
PROVIDERS: dict[str, dict] = {
    "DeepSeek":   {"base_url": "https://api.deepseek.com", "model": "deepseek-chat",
                   "concurrency": 8, "batch_size": 25},
    "通义千问":     {"base_url": "https://dashscope.aliyuncs.com/compatible-mode",
                   "model": "qwen-plus", "concurrency": 6, "batch_size": 25},
    "Kimi":       {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-32k",
                   "concurrency": 5, "batch_size": 25},
    # 修复：moonshot-v1-8k 仅 8k 上下文，配 batch_size=25 + max_tokens=8192 必超上下文
    #（system prompt + 25 条原文 + 输出预留之和远超 8192）→ 换 32k 型号（_8k 已弃用）
    "Ollama":     {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:7b",
                   "concurrency": 2, "batch_size": 12},
    "自定义":       {"base_url": "", "model": "", "concurrency": 5, "batch_size": 25},
}


def smart_defaults(provider: str) -> dict:
    """返回该厂商智能默认并发/批大小；未知厂商回退"自定义"。"""
    return PROVIDERS.get(provider, PROVIDERS["自定义"])


# 免费 API 平台预设（引擎三选一的第三选项：机翻 / 用户 API / 免费 API）。
# 这些平台注册即可拿免费 API Key（不花钱，限量/限速），OpenAI 兼容格式。
# 端点/模型经 2026 年搜索结果核实（见 providers.py 顶部注释来源）：
#   - 智谱 GLM-4-Flash：永久免费，官方承诺无限量（限 30 并发）
#   - 讯飞星火 Spark Lite：永久免费（限 QPS 2）
#   - 自定义免费：其他平台（如硅基流动/OpenRouter）端点用户自填
# 注意：用户仍需免费注册拿 key（key 走 keyring，不落盘）；本表只预设端点/模型/并发。
FREE_PROVIDERS: dict[str, dict] = {
    "智谱AI":   {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash",
                 "concurrency": 5, "batch_size": 25,
                 "note": "GLM-4-Flash 永久免费 · 需在 bigmodel.cn 免费注册拿 Key"},
    "讯飞星火":  {"base_url": "https://spark-api-open.xf-yun.com/v1", "model": "spark-lite",
                 "concurrency": 2, "batch_size": 12,
                 "note": "Spark Lite 永久免费（限速）· 需在讯飞开放平台免费注册拿 Key"},
    "自定义免费": {"base_url": "", "model": "", "concurrency": 3, "batch_size": 12,
                 "note": "其他免费平台（硅基流动/OpenRouter 等）端点自填"},
}


def free_defaults(provider: str) -> dict:
    """返回免费平台智能默认；未知免费平台回退「自定义免费」。"""
    return FREE_PROVIDERS.get(provider, FREE_PROVIDERS["自定义免费"])
