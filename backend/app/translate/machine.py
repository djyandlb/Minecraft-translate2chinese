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

from app.placeholder import protect, restore
from app.translate.common import should_translate

logger = logging.getLogger(__name__)

# MC 语言代码 → Google 语言代码映射（V3：多源可配置，默认 Google）。
# 修复（recheck）：未映射目标语言不再透传——透传会让 deep_translator 抛
# TargetLanguageNotFoundError，整批每条约一次网络白打、全失败回原文。map_lang 返回
# None 表示机翻不支持该语言 → translate_batch 提前整体标记失败（不逐条请求）。
_LANG_MAP = {
    "zh_cn": "zh-CN", "zh_tw": "zh-TW", "en_us": "en",
    "ja_jp": "ja", "ko_kr": "ko", "fr_fr": "fr", "de_de": "de",
    # 补常见语言（修复 recheck）：这些整合包常见语言原本会被透传崩溃整批失败
    "es_es": "es", "es_mx": "es", "es_ar": "es",
    "pt_br": "pt", "pt_pt": "pt", "it_it": "it", "ru_ru": "ru",
    "nl_nl": "nl", "pl_pl": "pl", "vi_vn": "vi", "th_th": "th",
    "id_id": "id", "tr_tr": "tr", "uk_ua": "uk",
}


def map_lang(mc_lang: str) -> str | None:
    """MC 语言代码 → Google 语言代码；未知返回 None（机翻不支持，调用方提前标记失败，
    不逐条白打网络）。"""
    if mc_lang not in _LANG_MAP:
        logger.warning("目标语言 %s 不在机翻映射表，机翻不支持该语言（返回 None）", mc_lang)
        return None
    return _LANG_MAP[mc_lang]


class MachineClient:
    """在线机翻（deep-translator 免费通道）。provider 参数预留多源切换。失败回原文。"""

    def __init__(self, provider: str = "google", concurrency: int = 5):
        self.provider = provider
        # 统一吞吐：并发跟随用户设置（设置页「并发数」）；Google 免费通道限流严，
        # 并发过高会全 429（反而更慢），cap 5（比 AI 引擎保守）
        self.concurrency = min(int(concurrency) or 5, 5)
        # 技术串过滤开关：默认 True（结构化 JSON/硬编码等 snake_case 标识符跳过）。
        # 语言文件阶段由 auto_flow 临时置 False——语言文件值是可翻译文本，
        # "Requires_Armor" 这类 snake_case 真实短语不得被 should_translate 误杀。
        self.filter_technical = True
        # 失败标记：机翻失败回原文时记录（修复：auto_flow 据此判真失败，不把「请求失败
        # 回原文」当「AI 故意保留」假成功——与 LLMClient 的 _batch_failed_texts 对齐）
        self._batch_failed_texts: set[str] = set()
        self._last_error_kind: str = "other"

    def set_throughput(self, concurrency: int | None = None, batch_size: int | None = None) -> bool:
        """热更新吞吐档位（v1.2.8）：machine 免费通道并发硬封顶 5（Google 免费限流，
        并发过高全 429 反而更慢）——改并发时也 cap，不因热更新绕过构造时的上限。
        与 LLMClient.set_throughput 同协议，供 auto_flow.set_throughput 统一调用。"""
        _changed = False
        if concurrency is not None:
            _c = min(max(1, int(concurrency)), 5)
            if _c != self.concurrency:
                self.concurrency = _c
                _changed = True
        if batch_size is not None:
            _b = max(1, int(batch_size))
            if _b != int(getattr(self, "batch_size", 0) or 0):
                self.batch_size = _b
                _changed = True
        return _changed

    async def translate_batch(self, texts: list[str], target_lang: str) -> list[str]:
        # 修复（recheck）：每次批处理清空失败标记——原实例属性跨批残留，同文本前批失败
        # 后批成功，残留标记会让上层 any(...) 误判把成功译文标 failed（LLM 已用 per-call ctx）
        self._batch_failed_texts = set()
        lang = map_lang(target_lang)
        if lang is None:
            # 机翻不支持该目标语言：整批标记失败回原文（不逐条网络白打）
            self._batch_failed_texts.update(t for t in texts
                                            if not (self.filter_technical and not should_translate(t)))
            self._last_error_kind = "unsupported"
            return list(texts)
        loop = asyncio.get_running_loop()
        results: list[str] = list(texts)
        todo = [(i, t) for i, t in enumerate(texts)
                if not (self.filter_technical and not should_translate(t))]
        # 修复：原串行 for 逐条 await → 20 条 20 次网络往返吞吐低；改并发限流（防 Google 限流）
        sem = asyncio.Semaphore(self.concurrency)

        async def _one(i: int, t: str) -> None:
            # 与 LLM 引擎对齐：机翻前先 protect 占位符（%s/{var}/§a/路径 token），
            # 防止 Google 把占位符翻坏；翻完 restore 还原。
            masked, markers = protect(t)
            try:
                async with sem:
                    # 调用点动态取 deep_translator.GoogleTranslator：若用 from-import 顶层绑定，
                    # 模块属性替换（monkeypatch）会失效；动态取可被测试覆盖
                    # 修复（recheck）：run_in_executor 无超时保护——Google 请求黑洞挂起时
                    # executor 线程永久阻塞、gather 永不返回 → 任务进度卡死。wait_for 超时
                    # 置该条失败保留原文（30s 对机翻单条足够）
                    translated = await asyncio.wait_for(
                        loop.run_in_executor(
                            None, deep_translator.GoogleTranslator(source="auto", target=lang).translate, masked),
                        timeout=30)
                results[i] = restore(translated, markers)
                # 修复（recheck）：成功重置错误类别——原代码只写失败分支，上批 timeout
                # 残留到本批，上层误显示「网络超时等待恢复」
                self._last_error_kind = "other"
            except asyncio.TimeoutError:
                # 修复：超时归 timeout（上层可重试）——原来全归 other 机翻网络失败零重试机会
                self._batch_failed_texts.add(t)
                self._last_error_kind = "timeout"
            except Exception:
                # 修复：记录失败原文（auto_flow 据此判真失败并记 failed），失败保留原文
                self._batch_failed_texts.add(t)
                self._last_error_kind = "other"

        await asyncio.gather(*(_one(i, t) for i, t in todo))
        return results
