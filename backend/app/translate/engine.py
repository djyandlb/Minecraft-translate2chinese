from typing import Protocol

from app.config import AppConfig
from app.translate.providers import smart_defaults


class TranslationEngine(Protocol):
    """翻译引擎统一接口。实现必须异步、保持顺序、失败回原文。"""
    async def translate_batch(self, texts: list[str], target_lang: str) -> list[str]: ...


def create_engine(cfg: AppConfig):
    """互斥工厂：engine == "llm" 走 LLMClient（keyring 读 key + 厂商智能默认），否则 MachineClient。
    注意：LLMClient/MachineClient 分别在任务 9/10 实现，本函数测试延后。"""
    if cfg.get("engine") == "llm":
        from app.translate.llm import LLMClient  # 任务 9 实现
        provider = cfg.get("provider", "DeepSeek")
        d = smart_defaults(provider)
        l = cfg.get("llm", {})
        import keyring
        api_key = keyring.get_password(cfg.get("api_key_ref", "mc-translator"), "api_key") or ""
        return LLMClient(
            l.get("base_url", "") or d["base_url"],
            api_key,
            l.get("model", "") or d["model"],
            concurrency=cfg.get("concurrency") or d["concurrency"],
            batch_size=cfg.get("batch_size") or d["batch_size"],
        )
    from app.translate.machine import MachineClient  # 任务 10 实现
    return MachineClient(cfg.get("machine", {}).get("provider", "google"))
