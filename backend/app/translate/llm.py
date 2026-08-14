"""OpenAI 兼容 /chat/completions LLM 翻译客户端（M2 核心）。

- V2 批次拼接翻译：多条文本拼一条 prompt（带 [iN] 标签），一次请求批量翻译。
- V2 降级链：整批请求失败 → 对半切批重试 → 逐条兜底，逐条失败回原文。
- V2 结果清洗：剥 Markdown 代码块 / "翻译："前缀 / 首尾引号。
- V3 token 统计：通过 on_usage(prompt_tokens, completion_tokens) 回调上报。

消费 common.should_translate 与 placeholder.protect/restore，
构造签名与任务 8 的 create_engine 调用匹配。
"""
import asyncio
import random
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

# 胡言乱语模式（silly_mode，用户诉求）：翻译风格指令——搞笑/玩梗/热梗，但必须忠实原意、
# 一一对应，禁止跑题/错译。占位符/ID/路径/专有名词保护规则不变。
_SILLY_NOTE = ("【胡言乱语模式】本条翻译用搞笑、玩梗、网络热梗的风格输出：可以俏皮、幽默、"
               "夸张，但**必须忠实传达原文语义**——每条译文都要一一对应回原文的意思，禁止跑题、"
               "禁止只玩梗不带原意、禁止错译。占位符/ID/路径/专有名词保护规则不变。")

# 助词折叠白名单：含连续重复格助词的合法成语（折叠前先保护，避免误伤）
_PARTICLE_IDIOMS = ("的的确确", "了了分明", "地地道道")

# 降级链深度封顶（v1.2.3 性能修复）：请求失败**不再无限对半切批**（原 25 条最坏
# 1+2+4+8+16≈31 次请求，不稳定 API 触发指数爆炸 → 1.5h 才几千条的根因）。
# - 网络类错误（timeout/network/ratelimit/server）：瞬态，切批无助于定位毒丸，最多切 1 层；
# - 非网络/整批格式不符：有界切批定位毒丸（深度 3，最坏 1+2+4+8=15 次）；
# - 深度封顶后剩余整块逐条记 ctx["failed"]（原始 source 文本，语义不变），
#   交上层 _translate_batch_pipeline 攒批重试（不破坏 meta 协议）。
_NET_KINDS = ("timeout", "network", "ratelimit", "server")
_MAX_NET_SPLIT_DEPTH = 1
_MAX_SPLIT_DEPTH = 3


def build_tagged_texts(texts: list[str]) -> str:
    """N 条文本拼一条 prompt，每行带 [i索引] 前缀，便于切回。
    原文真实换行必须转义为字面 \\n——否则换行会打断 [iN] 行式结构，
    后续裸行无编号，AI 只译 [i0] 首行就截断（用户实测长文本只出「（祭」）。"""
    return "\n".join(f"[i{i}] {t.replace(chr(10), '\\n')}" for i, t in enumerate(texts))


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
    """清洗 LLM 输出：剥代码块 / 翻译前缀 / 首尾引号 + 助词冗余安全清理。"""
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n", "", s)
        s = re.sub(r"\n?```$", "", s)
    s = re.sub(r"^(翻译|译文|结果|Translation)\s*[:：]\s*", "", s)
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'“”":
        s = s[1:-1]
    # 助词冗余安全清理（系统性修复「符文的的宝珠」）：连续重复格助词（的的/了了/地地/
    # 之之）→ 单助词。先保护白名单成语（的的确确/了了分明/地地道道）再折叠；
    # 字符集不含「得」——「得得」多是拟声词（马蹄声得得）合法（Agent 审查）。
    for _i, _ph in enumerate(_PARTICLE_IDIOMS):
        s = s.replace(_ph, f"\x00P{_i}\x00")
    s = re.sub(r"([的地之了])\1+", r"\1", s)
    for _i, _ph in enumerate(_PARTICLE_IDIOMS):
        s = s.replace(f"\x00P{_i}\x00", _ph)
    return s.strip()


class LLMClient:
    """OpenAI 兼容 /chat/completions 客户端。批次拼接翻译 + 降级链 + 失败回原文。"""

    def __init__(self, base_url: str, api_key: str, model: str,
                 concurrency: int = 5, batch_size: int = 10,
                 on_usage: Callable[[int, int], None] | None = None,
                 glossary_prompt: str = "",
                 silly_mode: bool = False):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.concurrency = concurrency
        self.batch_size = batch_size
        self.on_usage = on_usage
        self.glossary_prompt = glossary_prompt
        # 技术串过滤开关：默认 True（结构化 JSON/硬编码等 snake_case 标识符跳过）。
        # 语言文件阶段由 auto_flow 临时置 False——语言文件值是可翻译文本，
        # "Requires_Armor" 这类 snake_case 真实短语不得被 should_translate 误杀。
        self.filter_technical = True
        # 胡言乱语模式（用户诉求）：搞笑/热梗翻译但忠实原意，翻译 prompt 追加 _SILLY_NOTE
        self.silly_mode = silly_mode
        # 本批「请求失败回原文」的文本集合（区分「AI 故意保留原文」vs「API 失败/超时」）——
        # auto_flow 据此把真失败记 failed，不把 API 挂了的原文当「AI 保留」假成功（覆盖率问题）
        self._batch_failed_texts: set[str] = set()
        # 最近一次失败的类别：timeout/network/ratelimit/auth/other——
        # auto_flow 据此决定「等网络恢复重试」（timeout/network/ratelimit）还是「立即失败」（auth）
        self._last_error_kind: str = "other"
        self._client: httpx.AsyncClient | None = None
        # 鉴权致命错误（401/403）：置为非 None 后整批回原文并停止降级链递归，
        # translate_batch 结束后抛带文案异常（修复：失败指数放大 + 静默回原文）
        self._fatal_error: str | None = None
        # 运行时自适应（v1.2.3 熔断语义）：连续请求失败计数（429/超时/网络/server），
        # auto_flow._flush 据此自动降并发/批次——保护慢 API 不被高并发拖死（适配所有 API）。
        # 主请求成功清零；请求失败 +1。
        self._consec_fails: int = 0

    def _err_kind(self, exc: Exception) -> str:
        """异常分类（修复：4xx/5xx 曾一律归 other → 被 auto_flow 当网络超时无限等待）。

        可恢复（网络/服务临时问题，等待重试）：timeout / network / ratelimit / server(5xx)
        不可恢复（配置/数据错误，重试无用）：auth(401/403) / rejected(4xx 其余) / other
        """
        if isinstance(exc, httpx.TimeoutException):
            return "timeout"
        if isinstance(exc, httpx.TransportError):
            return "network"
        if isinstance(exc, httpx.HTTPStatusError):
            sc = exc.response.status_code if exc.response is not None else 0
            if sc in (401, 403):
                return "auth"
            if sc == 429:
                return "ratelimit"
            if 500 <= sc < 600:
                return "server"          # 服务端错误：临时可重试
            return "rejected"            # 4xx 请求/配置/数据错误：重试无用
        return "other"

    def _silly_note(self) -> str:
        """胡言乱语模式提示段：开启时拼进 system prompt（搞笑/热梗但保义）。"""
        return _SILLY_NOTE if self.silly_mode else ""

    def _zh_language_rules(self, target_lang: str) -> str:
        """目标语言翻译规则段（v1.1.0 重构）：zh 前缀注入**自然表达总原则**——删除逐条
        僵化规则/固定译法表（based off of→基于 等）/助词禁令/语序速查细则（用户反馈：
        「条条框框太多反而翻译不好」，机械规则限制 AI 自然表达）。质量问题（「的的」
        冗余/读不通/机械统一）由 AI 语境归一化审查（_ai_contextual_normalize）在语境中
        重翻修正，而非 prompt 硬约束。非中文目标只注入通用语序说明。"""
        if not target_lang.startswith("zh"):
            return "按该语言母语的自然语序重组词序与句法，不机械照搬英文语序。"
        return (
            "【表达要求】译文必须是地道自然的目标语言，按母语习惯重组语序与句法，"
            "禁止机械照搬英文词序、禁止机翻腔、禁止把英文单词硬插进中文短语；"
            "结合游戏界面语境（按钮/选项/提示/描述）意译，该口语就口语、该正式就正式；"
            "专有名词/特有名词在相同语境下译名保持一致；常用词（light/right/iron 等）"
            "按各自语境翻译，不要机械套用同一译名。"
        )

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            # 180s 超时（v1.2.3+ 慢 API 宽容：MiniMax/stepfun 等生成慢（单批 30-60s 正常），
            # 120s 对大批次长文本仍可能误超时 → 降级链把「慢」当「死」反复重试翻倍耗时）。
            # 生成慢是正常不是断网——超时只该在网络/服务挂掉时触发。
            self._client = httpx.AsyncClient(timeout=180, headers=headers)
        return self._client

    async def aclose(self) -> None:
        """关闭 httpx 连接池（P1-8）：任务结束释放，防长时间多任务 HTTP 连接堆积。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def generate_status(self, preset: str, context: str = "") -> str:
        """让 AI 生成一句当前运行状态描述（智能状态提示，用户诉求）。

        复用底层 /chat/completions：system 让模型当「状态播报员」，根据当前阶段写一句
        简短自然的中文状态（15 字内，不用句号）。用于 LLM 引擎阶段切换时的状态栏提示；
        机翻/无 AI 引擎没有此能力，由 auto_flow 用内置预设。失败/无 key/无地址回退 preset。
        """
        if not self.base_url or not self.api_key:
            return preset
        client = self._get_client()
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content":
                    "你是「像素译站」的状态播报员。根据给出的当前工作阶段，用简体中文写一句"
                    "状态描述，**最多 8 个汉字**（标点、省略号、符号不计入字数，可带省略号），"
                    "像给玩家的进度播报。直接输出这句话本身，不要解释、不要加引号。"
                    "例如：阶段『正在解压整合包』→『正在解压整合包…』；"
                    "阶段『正在翻译语言文件』→『翻译语言文件…』"},
                {"role": "user", "content": f"当前阶段：{preset}。{context}".strip()},
            ],
            "temperature": 0.7,
            "max_tokens": 60,   # 状态描述极短，防止模型长篇解释
        }
        try:
            resp = await client.post(f"{self.base_url}/chat/completions", json=body)
            resp.raise_for_status()
            data = resp.json()
            out = data["choices"][0]["message"]["content"]
            if self.on_usage:
                try:
                    u = data.get("usage") or {}
                    self.on_usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
                except Exception:
                    pass
            return (clean_translation(out) or preset)[:40]
        except Exception:
            return preset

    async def check_connectivity(self) -> bool:
        """连通性检查：轻量请求 API 服务，判断网络是否可达（网络超时的判定依据）。

        翻译请求失败时先做连通性检查：
        - 网络通（收到任何 HTTP 响应，含 4xx/5xx）→ 失败不是网络问题（API 服务异常/配置），
          调用方明确报错，不无限「等待网络恢复」假装网络超时（用户反馈「无论怎样都网络超时」根因）
        - 连不上（TransportError/Timeout）→ 真断网，才等网络恢复
        """
        try:
            client = self._get_client()
            # 轻量请求 GET /models（OpenAI 兼容端点）：收到响应即网络通
            await client.get(f"{self.base_url}/models", timeout=10)
            return True
        except (httpx.TransportError, httpx.TimeoutException):
            return False          # 连接失败/超时 → 网络不通
        except Exception:
            return True           # 收到响应但解析异常等 → 保守按网络通

    async def translate_batch(self, texts: list[str], target_lang: str,
                              forced: bool = False,
                              feedback: list[str] | None = None,
                              meta: dict | None = None) -> list[str]:
        """过滤不可翻译 → protect → 分批拼接 → 清洗还原。结果顺序与输入一致。

        forced=True（AI 审查漏翻重翻用）：prompt 追加「界面文本必须翻译」强化指令——
        普通翻译时 AI 可能保守保留原文（Waypoint Server/World 这类该翻没翻），
        审查找出漏翻后强制重翻，明确要求翻译用户可见的界面文本。

        feedback（审查反馈重翻）：list[str] 与 texts 对齐，
        每条是上次审查的不合格原因（空串 = 无）。prompt 对带原因的条目标注
        「上次审查不合格：[原因]」，让 AI **针对原因修正**，而非盲目重翻。
        """
        # 空配置校验：base_url 为空会把请求拼成 "/chat/completions" 抛 UnsupportedProtocol，
        # api_key 为空请求 401——两者都会整批静默回原文，用户得不到明确原因。
        # 这里直接抛带文案异常，调用方捕获后任务 failed 并向前端展示具体原因。
        if not self.base_url:
            raise ValueError("未配置 API 地址（base_url），请在设置页填写或选择厂商")
        if not self.api_key:
            raise ValueError("未配置 API Key，请在设置页填写")
        # 失败状态改为本次调用的局部 ctx（per-call 隔离）：翻译管道与审查重翻并发共享
        # 同一实例时，实例属性 clear()/置 None 会互相污染（请求失败被误判「AI 故意保留」
        # → 覆盖率 0 的假成功）。ctx 只随本次 translate_batch 走，经 meta 传出。
        ctx = {"failed": set(), "kind": "other", "fatal": None}
        results: list[str] = list(texts)
        protected = [protect(t) for t in texts]
        todo = [(i, t) for i, (t, _) in enumerate(zip(texts, protected))
                if (not self.filter_technical) or should_translate(t)]
        if todo:
            client = self._get_client()
            sem = asyncio.Semaphore(self.concurrency)
            chunks = [todo[k:k + self.batch_size] for k in range(0, len(todo), self.batch_size)]

            async def run_chunk(chunk):
                async with sem:
                    masked_list = [(i, protected[i][0]) for i, _ in chunk]
                    markers_list = [(i, protected[i][1]) for i, _ in chunk]
                    await self._request_chunk(client, chunk, masked_list, markers_list,
                                              target_lang, results, ctx, forced=forced,
                                              feedback=feedback)

            await asyncio.gather(*(run_chunk(c) for c in chunks))
            if ctx["fatal"]:
                raise ValueError(ctx["fatal"])
        if meta is not None:
            meta.update(ctx)
        return results

    async def _request_chunk(self, client, todo, masked_list, markers_list, target_lang, results,
                             ctx: dict, forced: bool = False, feedback: list[str] | None = None,
                             depth: int = 0):
        """对一批 todo 发一次请求；失败则有界切批重试（v1.2.3 深度封顶），最终逐条。

        todo 元素为 (原索引, 原文)，masked_list/markers_list 为
        (原索引, 脱敏文本/标记列表)，三者按块内顺序对齐。
        forced=True：prompt 追加「界面文本必须翻译」指令（AI 审查漏翻重翻用）。
        feedback：与 texts 全量对齐的不合格原因；对命中条目标注「上次审查不合格」，
        让 AI 针对原因修正（审查反馈重翻）。
        depth：降级切批深度（v1.2.3）。网络类错误 _MAX_NET_SPLIT_DEPTH=1 层，
        非网络/格式不符 _MAX_SPLIT_DEPTH=3 层；封顶后剩余整块记 ctx["failed"]
        交上层攒批重试，不再递归（消灭指数爆炸）。
        """
        if ctx["fatal"]:
            return   # 已遇鉴权致命错误：整批回原文，停止递归切批
        masked = [m for _, m in masked_list]
        markers = [m for _, m in markers_list]
        # feedback 按原索引对齐：todo 元素 (原索引, _)，feedback 与 texts 全量对齐
        fb_by_idx = {}
        if feedback:
            # 修复：feedback 与**全量** texts 对齐（feedback[orig_idx] 对应 texts[orig_idx]），
            # todo 是过滤后的子集（原索引不连续）——zip(todo, feedback) 会错位，
            # 每条拿到错误审查原因。按 orig_idx 直接索引 feedback。
            for orig_idx, _ in todo:
                if orig_idx < len(feedback) and feedback[orig_idx]:
                    fb_by_idx[orig_idx] = feedback[orig_idx]
        # 逐行构造：带审查原因的条目标注「上次审查不合格：[原因]」，让 AI 针对修正
        prompt_lines = []
        for i, ((orig_idx, _), m) in enumerate(zip(todo, masked)):
            line = f"[i{i}] {m.replace(chr(10), '\\n')}"   # 原文真实换行 → 字面 \n，防破坏 [iN] 行式
            if orig_idx in fb_by_idx:
                line += f"  ← 上次审查不合格：{fb_by_idx[orig_idx]}，请据此修正译文"
            prompt_lines.append(line)
        prompt = "\n".join(prompt_lines)
        # forced 追加段：审查发现漏翻的条目（该翻没翻的界面文本），强制要求翻译，
        # 但保留真专有名词/命令/纯代码标识的豁免，避免把 Minecraft 这类误翻。
        forced_note = ("\n【重要·强制翻译】这批条目中上次翻译保留了英文原文。它们大多是"
                       "用户可见的界面文本（选项名/按钮/设置/提示/说明等），**必须翻译成目标"
                       "语言**，禁止返回英文原文！只有原文确属专有名词（Minecraft、模组名）、"
                       "命令、纯代码标识/资源路径时才允许保留。拿不准时**宁可翻译，不要保留**"
                       "——保留原文会被判定为翻译失败。请逐条输出译文。"
                       if forced else "")
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content":
                    # 术语表注入：glossary_prompt 非空时拼到 system 提示词最前（任务 13）
                    (self.glossary_prompt + "\n" if self.glossary_prompt else "") +
                    forced_note +
                    f"把 Minecraft 游戏文本翻译成 {target_lang}。输入每行以 [i数字] 开头，"
                    f"你必须严格按输入行数逐行输出译文，一行对应一条，不得遗漏、不得合并、不得截断——"
                    f"每条必须**完整翻译整段**：长句、多行文本（用 \\n 表示的换行）必须整段翻完，"
                    f"禁止只译开头就停止、禁止英文原文残留；译文**只允许目标语言**（专有名词/命令/"
                    f"占位符等前述豁免除外），不得把英文原文原样输出；多行原文的译文用 \\n 保持同样"
                    f"换行结构；不得添加任何解释/前缀/编号说明。译文须能作为游戏内显示文本，"
                    f"保留 %s/%d/%n 等占位符原样。"
                    f"**核心原则：用户可见的英文一律翻译成目标语言**——按钮、选项、设置项、提示、"
                    f"描述、物品、技能、状态效果等，无论一个英文单词还是整句都必须翻译；单个普通"
                    f"英文单词/短词（Sprint→冲刺、Bombs→炸弹、Plantkillable→可杀植物、"
                    f"Enabled→已启用、Sneak→潜行）同样必须翻译成目标语言的自然词，"
                    f"不得因「像术语/像物品名/像技能名」就保留原文，不得只改大小写或原样照抄；"
                    f"**宁可翻译，不要保留**——保留英文原文会被判定为翻译失败。"
                    f"但**代码标识一律保留，绝不翻译**：类名/方法名/变量名/字段名/注册 ID/命令/"
                    f"资源路径/文件名/按键名/着色器变量/本地化键等，无论多短（如 Renderer、"
                    f"getPlayer、isAlive、minecraft:diamond、/give、key.forward）都保持原样——"
                    f"只有用户可见文本（语言文件值、界面文字）中的短词才必须翻译，代码标识靠形态"
                    f"（驼峰/下划线/点分/斜杠/前缀）与普通文本区分。"
                    f"仅以下确定性情况允许保留英文：真实的游戏/模组专有名词（Minecraft、JEI、"
                    f"Xaero、Balm 等模组名）、命令（/give @p diamond）、资源路径/文件名"
                    f"（config/jei/jei.toml）、纯代码标识（类名/变量名/注册 ID/本地化键，"
                    f"如 com.example.Mod、minecraft:diamond）。罗马数字（I、II、III、IV、V、"
                    f"VI、VII、VIII、IX、X 等）保持原样不翻译。"
                    f"下划线连接词按语境判断：明显是 ID/变量（zi_min、player_name）保留；"
                    f"若下划线词是真实显示短语（No_Minimap、Craft_Table）则必须翻译"
                    f"（→ 无小地图、工作台）。"
                    f"斜杠（/）分隔的两个普通英文单词（如 world/server、Raining/Snowing、"
                    f"background/shadow）是并列选项不是路径，**必须翻译**成中文并列词"
                    f"（世界/服务器、下雨/下雪、背景/阴影），禁止保留英文。"
                    f"effect.* 键值是状态效果名：部分 mod（尤其地图/界面类，如 Xaero）会把效果名"
                    f"拼进资源路径 Identifier（只允许 a-z0-9/._- 字符），翻译成目标语言含非法字符"
                    f"会致游戏崩溃。请你判断每条 effect 名：若可能被用于资源定位，保留英文原样"
                    f"逐字输出原文（不加任何说明/括号）；纯显示的效果名翻译成目标语言。"
                    f"译文必须贴合 {target_lang} 母语者的自然表达习惯，**按目标语言的自然语序重组"
                    f"词序与句法**。"
                    f"{self._zh_language_rules(target_lang)}"
                    f"若 prompt 提供了「已确认专名对照」，只在语境匹配时遵循。"
                    f"输出保持 [i数字] 前缀和 %%MC_数字%% 占位符原样，只输出译文。" +
                    self._silly_note()},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 8192,   # 防默认上限截断输出（截断会让后半批缺失/句子切半，触发「未返回文本」）
        }
        try:
            resp = await client.post(f"{self.base_url}/chat/completions", json=body)
            resp.raise_for_status()
            data = resp.json()
            out = data["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as e:
            sc = e.response.status_code if e.response is not None else 0
            if sc in (401, 403):
                # 鉴权致命错误：置 fatal 停止降级链（防失败指数放大），translate_batch 收尾抛明确异常
                ctx["fatal"] = f"API Key 无效或无权限（HTTP {sc}），请检查设置中的 API Key"
                return
            ctx["kind"] = self._err_kind(e)   # 修复：batch 级失败也更新错误类别（否则错误分类漂移）
            out = None
        except Exception as e:
            ctx["kind"] = self._err_kind(e)   # 修复：同上
            out = None
        if out is not None:
            # 修复：on_usage 回调（含落盘）异常只记日志，不影响已成功的翻译结果
            if self.on_usage:
                try:
                    u = data.get("usage") or {}
                    self.on_usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
                except Exception:
                    pass
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
                # 整批格式完全不符（一个条目都没解析出来）→ 有界切批重试（v1.2.3 深度封顶）。
                # 透传 forced/feedback：审查反馈重翻在递归降级时不丢原因标注
                if len(todo) > 1 and depth < _MAX_SPLIT_DEPTH:
                    await asyncio.sleep(0.5 * random.uniform(0.8, 1.2))  # 抖动退避防惊群
                    half = len(todo) // 2
                    await self._request_chunk(client, todo[:half], masked_list[:half], markers_list[:half],
                                              target_lang, results, ctx, forced=forced, feedback=feedback,
                                              depth=depth + 1)
                    await self._request_chunk(client, todo[half:], masked_list[half:], markers_list[half:],
                                              target_lang, results, ctx, forced=forced, feedback=feedback,
                                              depth=depth + 1)
                else:
                    # 深度封顶：剩余整块不再递归，记 failed 交上层攒批重试
                    for _i, _t in todo:
                        ctx["failed"].add(_t)
                return
            for n, (i, _) in enumerate(todo):
                if n in parsed:
                    results[i] = restore(clean_translation(parsed[n]), markers[n])
                else:
                    # 该条漏标 → 逐条降级 _translate_single，不静默保留原文（P0 根因 2）。
                    results[i] = await self._translate_single(
                        client, todo[n][1], masked[n], markers[n], target_lang, ctx,
                        forced=forced, feedback=(fb_by_idx.get(todo[n][0], "") if feedback else ""))
            return
        # 请求失败 → 降级：有界切批（v1.2.3 深度封顶，不再无限切到单条——指数爆炸）。
        # 网络类错误（timeout/network/ratelimit/server）是瞬态，切批无助于定位毒丸，
        # 只放大请求量 → 最多切 1 层；非网络/格式不符切 3 层定位毒丸后整块交上层攒批重试。
        # 切批前抖动退避（0.4-0.6s）让网络有恢复窗口、防惊群。
        if len(todo) > 1:
            _kind = ctx.get("kind") or "other"
            _max_depth = _MAX_NET_SPLIT_DEPTH if _kind in _NET_KINDS else _MAX_SPLIT_DEPTH
            if depth < _max_depth:
                await asyncio.sleep(0.5 * random.uniform(0.8, 1.2))
                half = len(todo) // 2
                await self._request_chunk(client, todo[:half], masked_list[:half], markers_list[:half],
                                          target_lang, results, ctx, forced=forced, feedback=feedback,
                                          depth=depth + 1)
                await self._request_chunk(client, todo[half:], masked_list[half:], markers_list[half:],
                                          target_lang, results, ctx, forced=forced, feedback=feedback,
                                          depth=depth + 1)
            else:
                # 深度封顶：剩余整块不再递归，逐条记 failed（交上层 _flush 攒批重试）
                for _i, _t in todo:
                    ctx["failed"].add(_t)
        else:
            i, t = todo[0]
            results[i] = await self._translate_single(
                client, t, masked[0], markers[0], target_lang, ctx,
                forced=forced, feedback=(fb_by_idx.get(i, "") if feedback else ""))

    async def _translate_single(self, client, text, masked, markers, target_lang, ctx: dict,
                                forced: bool = False, feedback: str = ""):
        """逐条兜底：单条请求，失败回原文。

        forced/feedback（审查反馈重翻）：与 _request_chunk 一致——强制翻译界面文本、
        携带审查不合格原因让 AI 针对修正（递归/降级链透传，不丢标注）。
        """
        # forced/feedback 注入：单条也保留「界面文本必须翻译」+「审查原因修正」指令
        forced_note = ("【重要·强制翻译】这是用户可见的界面文本，**必须翻译成目标语言**，"
                       "禁止返回英文原文！只有确属专有名词/命令/代码标识/资源路径才可保留；"
                       "拿不准时**宁可翻译，不要保留**（保留原文会被判定为翻译失败）。"
                       if forced else "")
        fb_note = (f"【上次审查不合格】{feedback}，请据此修正译文，翻译到合格为止。"
                   if feedback else "")
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content":
                    # 术语表注入：glossary_prompt 非空时拼到 system 提示词最前（任务 13）
                    (self.glossary_prompt + "\n" if self.glossary_prompt else "") +
                    forced_note + fb_note +
                    f"把 Minecraft 游戏文本翻译成 {target_lang}。保留 %s/%d/%n 等占位符与 "
                    f"%%MC_数字%% 占位符原样；必须译完整句，禁止截断/只译前半句；"
                    f"斜杠（/）分隔的普通英文单词（如 world/server、Raining/Snowing）是并列选项，"
                    f"**必须翻译**成中文并列词（世界/服务器、下雨/下雪），禁止保留英文；"
                    f"真正的路径/命令保留原样；effect.* 效果名若会被用于拼资源路径 Identifier"
                    f"（如 Xaero 地图类）保留英文原样，纯显示的效果名翻译成目标语言；"
                    f"下划线连接的单词（如 zi_min、player_name）是"
                    f"玩家名/ID，保留原样不翻译；专有名词（模组名/类名/API/命令名）保留原文，"
                    f"其余一律翻译成目标语言；译文必须贴合 {target_lang} 母语者的自然表达习惯，"
                    f"**按目标语言的自然语序重组词序与句法**（不限于中文，日文/韩文等同样按各自"
                    f"母语语序，禁止机械照搬英文词序；示例·中文目标：Blue Cyber Lamp → 「蓝色赛博灯」"
                    f"而非「赛博灯蓝色」）；结合游戏界面语境意译，不要逐词直译、"
                    f"不要书面翻译腔，读起来像原生中文；禁止把英文单词硬插进中文短语，"
                    f"能意译的英文一律意译，避免中英混杂；"
                    f"专有名词/特有名词在相同语境下译名统一，常用词（light/right 等）按各自语境"
                    f"翻译，禁止机械套用同一译名；若 prompt 提供了「已确认专名对照」，只在语境"
                    f"匹配时遵循；"
                    f"原文含换行时用 \\n 表示，只输出译文，不要解释。" +
                    self._silly_note()},
                {"role": "user", "content": masked},
            ],
            "temperature": 0.2,
            "max_tokens": 8192,   # 防默认上限截断输出（单条长句也要完整译出）
        }
        if ctx["fatal"]:
            # 鉴权致命错误后：不再发请求，标记失败并回原文（auto_flow 据此整批失败）
            ctx["failed"].add(text)
            return text
        try:
            resp = await client.post(f"{self.base_url}/chat/completions", json=body)
            resp.raise_for_status()
            data = resp.json()
            out = data["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as e:
            ctx["kind"] = self._err_kind(e)
            sc = e.response.status_code if e.response is not None else 0
            if sc in (401, 403):
                ctx["fatal"] = f"API Key 无效或无权限（HTTP {sc}），请检查设置中的 API Key"
            ctx["failed"].add(text)
            return text
        except Exception as exc:
            # 网络超时/连接/其他：分类记录，auto_flow 据此等网络恢复重试（不记 failed）
            ctx["kind"] = self._err_kind(exc)
            ctx["failed"].add(text)
            return text
        # 兜底容错：即便模型带 [iN] 前缀输出也剥掉（单条无标签时直接取全文）。
        single_parsed = parse_tagged(out)
        if 0 in single_parsed:
            out = single_parsed[0]
        # 修复：on_usage 回调（含落盘）异常只记日志，不影响已成功的翻译结果
        if self.on_usage:
            try:
                u = data.get("usage") or {}
                self.on_usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
            except Exception:
                pass
        return restore(clean_translation(out), markers)
