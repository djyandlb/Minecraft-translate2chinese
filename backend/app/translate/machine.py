"""在线机翻客户端（deep-translator 免费通道）。

- V3 决策：多源可配置（provider 参数），默认 Google。
- 与任务 9 的 LLMClient 实现同一个 translate_batch 协议，
  供任务 8 的 create_engine 在 engine == "machine" 时返回。
- 消费 common.should_translate：技术串跳过不翻。
- 逐条 Google 翻译走 run_in_executor，不阻塞事件循环；失败回原文。
"""
import asyncio
import logging

import deep_translator

from app.translate.common import should_translate

logger = logging.getLogger(__name__)

# MC 语言代码 → Google 语言代码映射（V3：多源可配置，默认 Google）
_LANG_MAP = {
    "zh_cn": "zh-CN", "zh_tw": "zh-TW", "en_us": "en",
    "ja_jp": "ja", "ko_kr": "ko", "fr_fr": "fr", "de_de": "de",
}


def map_lang(mc_lang: str) -> str:
    """MC 语言代码 → Google 语言代码；未知原样返回。"""
    if mc_lang not in _LANG_MAP:
        # 目标语言不在映射表：打 warning 提示，但仍按原样透传，交给下游翻译兜底
        logger.warning("目标语言 %s 不在机翻映射表，按原样透传", mc_lang)
    return _LANG_MAP.get(mc_lang, mc_lang)


class MachineClient:
    """在线机翻（deep-translator 免费通道）。provider 参数预留多源切换。失败回原文。"""

    def __init__(self, provider: str = "google"):
        self.provider = provider

    async def translate_batch(self, texts: list[str], target_lang: str) -> list[str]:
        lang = map_lang(target_lang)
        loop = asyncio.get_running_loop()
        results: list[str] = []
        for t in texts:
            if not should_translate(t):
                results.append(t)
                continue
            try:
                # 调用点动态取 deep_translator.GoogleTranslator：若用 from-import 顶层绑定，
                # 模块属性替换（monkeypatch）会失效；动态取可被测试覆盖
                results.append(await loop.run_in_executor(
                    None, deep_translator.GoogleTranslator(source="auto", target=lang).translate, t))
            except Exception:
                results.append(t)
        return results
