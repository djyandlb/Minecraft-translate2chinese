"""OpenAI 兼容 /chat/completions LLM 翻译客户端（M2 核心）。

- V2 批次拼接翻译：多条文本拼一条 prompt（带 [iN] 标签），一次请求批量翻译。
- V2 降级链：整批请求失败 → 对半切批重试 → 逐条兜底，逐条失败回原文。
- V2 结果清洗：剥 Markdown 代码块 / "翻译："前缀 / 首尾引号。
- V3 token 统计：通过 on_usage(prompt_tokens, completion_tokens) 回调上报。

消费 common.should_translate 与 placeholder.protect/restore，
构造签名与任务 8 的 create_engine 调用匹配。
"""
import asyncio
import re
from typing import Callable

import httpx

from app.translate.common import should_translate
from app.placeholder import protect, restore

_TAG_RE = re.compile(r"^\[i(\d+)\]\s*", re.MULTILINE)


def build_tagged_texts(texts: list[str]) -> str:
    """N 条文本拼一条 prompt，每行带 [i索引] 前缀，便于切回。"""
    return "\n".join(f"[i{i}] {t}" for i, t in enumerate(texts))


def parse_tagged(translated: str) -> dict[int, str]:
    """解析模型按 [iN] 标签输出的结果。"""
    out: dict[int, str] = {}
    cur_idx: int | None = None
    parts: list[str] = []
    for line in translated.splitlines():
        m = _TAG_RE.match(line)
        if m:
            if cur_idx is not None:
                out[cur_idx] = "\n".join(parts).strip()
            cur_idx, parts = int(m.group(1)), [line[m.end():].strip()]
        elif cur_idx is not None:
            parts.append(line)
    if cur_idx is not None:
        out[cur_idx] = "\n".join(parts).strip()
    return out


def clean_translation(raw: str) -> str:
    """清洗 LLM 输出：剥代码块 / 翻译前缀 / 首尾引号。"""
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n", "", s)
        s = re.sub(r"\n?```$", "", s)
    s = re.sub(r"^(翻译|译文|结果|Translation)\s*[:：]\s*", "", s)
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'“”":
        s = s[1:-1]
    return s.strip()


class LLMClient:
    """OpenAI 兼容 /chat/completions 客户端。批次拼接翻译 + 降级链 + 失败回原文。"""

    def __init__(self, base_url: str, api_key: str, model: str,
                 concurrency: int = 5, batch_size: int = 20,
                 on_usage: Callable[[int, int], None] | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.concurrency = concurrency
        self.batch_size = batch_size
        self.on_usage = on_usage
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            self._client = httpx.AsyncClient(timeout=60, headers=headers)
        return self._client

    async def translate_batch(self, texts: list[str], target_lang: str) -> list[str]:
        """过滤不可翻译 → protect → 分批拼接 → 清洗还原。结果顺序与输入一致。

        可翻译条目按 batch_size 切块，每块一次请求（块内用 [iN] 标签对应回原索引）。
        """
        results: list[str] = list(texts)
        protected = [protect(t) for t in texts]
        todo = [(i, t) for i, (t, _) in enumerate(zip(texts, protected)) if should_translate(t)]
        if todo:
            client = self._get_client()
            sem = asyncio.Semaphore(self.concurrency)
            chunks = [todo[k:k + self.batch_size] for k in range(0, len(todo), self.batch_size)]

            async def run_chunk(chunk):
                async with sem:
                    masked_list = [(i, protected[i][0]) for i, _ in chunk]
                    markers_list = [(i, protected[i][1]) for i, _ in chunk]
                    await self._request_chunk(client, chunk, masked_list, markers_list,
                                              target_lang, results)

            await asyncio.gather(*(run_chunk(c) for c in chunks))
        return results

    async def _request_chunk(self, client, todo, masked_list, markers_list, target_lang, results):
        """对一批 todo 发一次请求；失败则对半切批重试，最终逐条。

        todo 元素为 (原索引, 原文)，masked_list/markers_list 为
        (原索引, 脱敏文本/标记列表)，三者按块内顺序对齐。
        """
        masked = [m for _, m in masked_list]
        markers = [m for _, m in markers_list]
        prompt = build_tagged_texts(masked)
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content":
                    f"把 Minecraft 游戏文本翻译成 {target_lang}。每行以 [i数字] 开头，"
                    f"输出保持 [i数字] 前缀和 %%MC_数字%% 占位符原样，只输出译文，不要解释。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
        try:
            resp = await client.post(f"{self.base_url}/chat/completions", json=body)
            resp.raise_for_status()
            data = resp.json()
            out = data["choices"][0]["message"]["content"]
            if self.on_usage:
                u = data.get("usage") or {}
                self.on_usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
        except Exception:
            out = None
        if out is not None:
            parsed = parse_tagged(out)
            for n, (i, _) in enumerate(todo):
                if n in parsed:
                    results[i] = restore(clean_translation(parsed[n]), markers[n])
            return
        # 请求失败 → 降级：对半切批，直到逐条
        if len(todo) > 1:
            half = len(todo) // 2
            await self._request_chunk(client, todo[:half], masked_list[:half], markers_list[:half],
                                      target_lang, results)
            await self._request_chunk(client, todo[half:], masked_list[half:], markers_list[half:],
                                      target_lang, results)
        else:
            i, t = todo[0]
            results[i] = await self._translate_single(client, t, masked[0], markers[0], target_lang)

    async def _translate_single(self, client, text, masked, markers, target_lang):
        """逐条兜底：单条请求，失败回原文。"""
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content":
                    f"把 Minecraft 游戏文本翻译成 {target_lang}。保留 %%MC_数字%% 占位符原样，只输出译文。"},
                {"role": "user", "content": masked},
            ],
            "temperature": 0.2,
        }
        try:
            resp = await client.post(f"{self.base_url}/chat/completions", json=body)
            resp.raise_for_status()
            data = resp.json()
            out = data["choices"][0]["message"]["content"]
            if self.on_usage:
                u = data.get("usage") or {}
                self.on_usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
            return restore(clean_translation(out), markers)
        except Exception:
            return text
