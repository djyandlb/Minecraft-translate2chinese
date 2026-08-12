from typing import Protocol

from app.config import AppConfig
from app.translate.providers import free_defaults, smart_defaults


def _clamp_int(v, default: int, lo: int, hi: int) -> int:
    """并发/批大小取值收敛：负数/非数字/超界 → 落到 [lo, hi]（修复 recheck：负数
    concurrency 会让 asyncio.Semaphore(-3) 抛 ValueError 任务直接 failed；字符串 "8"
    抛 TypeError；batch_size 负数导致 range 空切片静默不翻）。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(n, hi))


class TranslationEngine(Protocol):
    """翻译引擎统一接口。实现必须异步、保持顺序、失败回原文。"""
    async def translate_batch(self, texts: list[str], target_lang: str) -> list[str]: ...


def create_engine(cfg: AppConfig):
    """互斥工厂（三选项）：engine == "llm" 走用户配置的 LLM API；engine == "free" 走
    免费 API 平台预设（智谱/讯飞等，注册免费拿 key）；否则 MachineClient（在线机翻）。
    llm 与 free 都构造 LLMClient，仅厂商默认表不同（PROVIDERS vs FREE_PROVIDERS）。
    注意：LLMClient/MachineClient 分别在任务 9/10 实现，本函数测试延后。
    """
    from app.translate.llm import LLMClient  # 任务 9 实现
    from app.translate.machine import MachineClient  # 任务 10 实现

    engine = cfg.get("engine")
    if engine in ("llm", "free"):
        if engine == "llm":
            provider = cfg.get("provider", "DeepSeek")
            d = smart_defaults(provider)
        else:
            provider = cfg.get("provider", "智谱AI")
            d = free_defaults(provider)
        l = cfg.get("llm", {})
        base_url = (l.get("base_url") or d["base_url"] or "").strip()
        model = (l.get("model") or d["model"] or "").strip()
        try:
            import keyring
            api_key = keyring.get_password(cfg.get("api_key_ref", "mc-translator"), "api_key") or ""
        except Exception:
            # headless/CI/无凭证服务环境：读不到 key 不崩，交给下游「空 key 请求 401」
            # 处理，任务失败信息能明确指出是 key 问题
            api_key = ""
        # 空 base_url/model 允许构造（自定义免费平台用户自填端点的合法场景），
        # 但请求入口 LLMClient.translate_batch 会校验并抛带文案异常，避免把请求
        # 拼成 "/chat/completions" 静默降级回原文
        return LLMClient(
            base_url,
            api_key,
            model,
            concurrency=_clamp_int(cfg.get("concurrency"), d["concurrency"], 1, 64),
            batch_size=_clamp_int(cfg.get("batch_size"), d["batch_size"], 1, 200),
            silly_mode=bool(cfg.get("silly_mode")),   # 胡言乱语模式（搞笑/热梗翻译但保义）
        )
    return MachineClient(cfg.get("machine", {}).get("provider", "google"),
                         concurrency=_clamp_int(cfg.get("concurrency"), 5, 1, 5))
