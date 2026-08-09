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

# 行首条目前缀：兼容 [iN]、**iN**、*iN* 与「数字. / 数字、 / 数字)」编号。
# 数字编号按 1-based（1. → 索引 0），[iN] 按 0-based。
_PREFIX_RE = re.compile(
    r"^(?:\[i(\d+)\]|\*\*i(\d+)\*\*|\*i(\d+)\*|(\d+)\s*[.、)）])\s*",
    re.MULTILINE,
)
# 行内标签（合并行切分）：仅 [iN] 形式，避免误伤普通文本里的数字编号。
_INLINE_TAG_RE = re.compile(r"\[i(\d+)\]")
# 末尾说明行（无编号前缀的总结语）：解析时忽略。
_NOTE_RE = re.compile(r"^(以上|以下|翻译完成|译文完|全部译文|完|仅此|共\s*\d+\s*条)")


def build_tagged_texts(texts: list[str]) -> str:
    """N 条文本拼一条 prompt，每行带 [i索引] 前缀，便于切回。"""
    return "\n".join(f"[i{i}] {t}" for i, t in enumerate(texts))


def _index_from_match(m: re.Match) -> int:
    """从行首前缀匹配提取条目索引：[iN]/**iN**/*iN* 按 N，数字. 按 N-1。"""
    for g in (1, 2, 3):
        if m.group(g) is not None:
            return int(m.group(g))
    return max(0, int(m.group(4)) - 1)


def parse_tagged(translated: str) -> dict[int, str]:
    """解析模型按 [iN] 标签（或数字. / **iN** 变体）输出的结果。

    容错（P0 止血）：
      - 行首前缀接受 [iN]、**iN**、*iN*、数字. 等形态；
      - 合并行切分：`[i0] 甲 [i1] 乙` 拆成两条；
      - 忽略末尾「以上是全部译文」类无编号说明行。
    """
    out: dict[int, str] = {}
    cur_idx: int | None = None
    parts: list[str] = []

    def _flush() -> None:
        nonlocal cur_idx, parts
        if cur_idx is not None:
            out[cur_idx] = "\n".join(parts).strip()
        cur_idx, parts = None, []

    for line in translated.splitlines():
        m = _PREFIX_RE.match(line)
        if m:
            _flush()
            cur_idx = _index_from_match(m)
            rest = line[m.end():]
            # 合并行切分：行内若还有 [iN]，逐段拆出。
            pos = 0
            for im in _INLINE_TAG_RE.finditer(rest):
                seg = rest[pos:im.start()]
                if seg.strip():
                    parts.append(seg.strip())
                pos = im.end()
                _flush()                      # 结算当前条目，开启新条目
                cur_idx = int(im.group(1))
            tail = rest[pos:]
            if tail.strip():
                parts.append(tail.strip())
        else:
            if cur_idx is None:
                continue
            if _NOTE_RE.match(line.strip()):
                continue                      # 末尾说明行忽略
            parts.append(line)
    _flush()
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
                 on_usage: Callable[[int, int], None] | None = None,
                 glossary_prompt: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.concurrency = concurrency
        self.batch_size = batch_size
        self.on_usage = on_usage
        self.glossary_prompt = glossary_prompt
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
                    # 术语表注入：glossary_prompt 非空时拼到 system 提示词最前（任务 13）
                    (self.glossary_prompt + "\n" if self.glossary_prompt else "") +
                    f"把 Minecraft 游戏文本翻译成 {target_lang}。输入每行以 [i数字] 开头，"
                    f"你必须严格按输入行数逐行输出译文，一行对应一条，不得遗漏、不得合并、"
                    f"不得添加任何解释/前缀/编号说明。译文须能作为游戏内显示文本，"
                    f"保留 %s/%d/%n 等占位符原样；原文含换行时用 \\n 表示，不要真的换行拆条。"
                    f"输出保持 [i数字] 前缀和 %%MC_数字%% 占位符原样，只输出译文。"},
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
            # 单条（len(todo)==1）：模型通常不带 [iN] 标签直接输出译文 → 直接采用全文。
            # 若仍带 [i0] 前缀（旧行为）则剥标签后再采用（P0 根因 1）。
            if len(todo) == 1:
                i, _ = todo[0]
                single_parsed = parse_tagged(out)
                if 0 in single_parsed:
                    results[i] = restore(clean_translation(single_parsed[0]), markers[0])
                else:
                    results[i] = restore(clean_translation(out), markers[0])
                return
            parsed = parse_tagged(out)
            if not parsed:
                # 整批格式完全不符（一个条目都没解析出来）→ 对半切批重试（复用现有降级链）。
                half = len(todo) // 2
                await self._request_chunk(client, todo[:half], masked_list[:half], markers_list[:half],
                                          target_lang, results)
                await self._request_chunk(client, todo[half:], masked_list[half:], markers_list[half:],
                                          target_lang, results)
                return
            for n, (i, _) in enumerate(todo):
                if n in parsed:
                    results[i] = restore(clean_translation(parsed[n]), markers[n])
                else:
                    # 该条漏标 → 逐条降级 _translate_single，不静默保留原文（P0 根因 2）。
                    results[i] = await self._translate_single(
                        client, todo[n][1], masked[n], markers[n], target_lang)
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
                    # 术语表注入：glossary_prompt 非空时拼到 system 提示词最前（任务 13）
                    (self.glossary_prompt + "\n" if self.glossary_prompt else "") +
                    f"把 Minecraft 游戏文本翻译成 {target_lang}。保留 %s/%d/%n 等占位符与 "
                    f"%%MC_数字%% 占位符原样；原文含换行时用 \\n 表示，只输出译文，不要解释。"},
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
            # 兜底容错：即便模型带 [iN] 前缀输出也剥掉（单条无标签时直接取全文）。
            single_parsed = parse_tagged(out)
            if 0 in single_parsed:
                out = single_parsed[0]
            return restore(clean_translation(out), markers)
        except Exception:
            return text
