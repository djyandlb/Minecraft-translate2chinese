# -*- coding: utf-8 -*-
"""AI 译文质量审查（AI 裁判核心）：翻译后逐批审查译文质量，不合格条目交调用方强制重翻。

规则审计（audit.py：占位符/官方术语/键名语义/重复译法）覆盖**机械不变量**，但判断不了
译文质量本身——Xaero 审查报告实测的「句子截断 / 中英混杂 / 生硬 / 偏离原文」规则抓不出来。
AI 审查补上这一层：LLM 语义判断「译文是否忠实（无截断/丢词）+ 通顺（原生目标语言、
无中英混杂）+ 完整（整句译出）」，不合格条目带原因返回，由 auto_flow 强制重翻。

成本控制：审查只判断不翻译，每批 30 条（比翻译批次大），分页并发；大型 mod 几千条
译文 ≈ 几十次审查请求，与翻译批次数同量级，可接受。
"""
import asyncio
import re

from typing import Callable

# 每批审查条数（判断比翻译轻，批次可更大）；并发与翻译解耦（审查占 token 少）
_REVIEW_PAGE = 30
_REVIEW_CONCURRENCY = 4

# 行首 [iN] 前缀（兼容 *iN* / **iN** 变体 / 「N. N、 N)」数字编号——1-based 需转 0-based）
_PREFIX_RE = re.compile(r"^\s*(?:\[i(\d+)\]|\*i(\d+)\*|\*\*i(\d+)\*\*|(\d+)\s*[.、)）])\s*")
# 整批合格标记（无不合格条目）
_DONE_RE = re.compile(r"^(全部合格|无不合格|质量合格|均合格|皆合格|都合格|合格)", re.I)

# 审查标准（prompt 主干的换行列表，供 _review_prompt 拼装）。
# 收敛（修复：审查吹毛求疵把「路径点颜色→Waypoint Color」等合理译文误判不合格）：
# 只标记**明显**质量缺陷，严禁对「措辞略有出入但有多种自然译法」的合理翻译下重手。
_REVIEW_RULES = """不合格标准（仅限**明显**质量缺陷，任一即标记）：
1. 截断/不完整：译文只译了部分，后半句是英文原文残留或缺失
2. 中英混杂：英文单词硬插进中文短语（技术名词/模组名/命令名/按键名/单位缩写除外）
3. 严重偏离：丢词、改义、与源文本意思明显不符。
   **张冠李戴/串位（译文描述的内容与源文本完全无关，像是另一条条目的译文——
   例如源文本是附魔说明、译文却是完全不相干的物品描述）必标记**（v1.3.6，
   用户实测翻译置换：A 条目译文被标到 B 条目 key 上，靠占位符交叉校验 + 此规则双保险）
4. 生硬难读：机翻腔、语序混乱、读不通顺
5. 漏翻：译文与源文本完全相同（未翻译），且源文本是**用户可见的界面文本**
   （按钮/选项/设置项描述/提示等），本应翻译成目标语言。
   **单个普通英文单词/短词（如 Bombs、Sprint、Plantkillable、Enabled）未翻译
   同样算漏翻**——只有真正的游戏/模组专有名词（Minecraft、Xaero）才可保留
6. 助词冗余：译文出现明显多余的「的/地/得」助词，读不通顺——含连续重复
   （的的/了了/地地/得得/之之，如「符文的的宝珠」）或动词短语误加「的」
   （based off of 应译「基于」而非「基于的」，如「基于的附魔的过滤器」）
   ——「的」只用于必要所属/修饰，明显冗余即标记

严禁对以下情况标记不合格（这是正常翻译，不是缺陷）：
- **译文忠实、意思正确、读得通，即使措辞与另一种自然译法略有出入——绝不标记**
  （如 Legacy→遗留、Dimension Selection→维度选择、No WM Cave Maps→无世界地图洞穴地图
  都是正确翻译，措辞可优化但不算不合格）
- 专有名词（Minecraft/模组名）、命令、斜杠并列词（world/server）按规则保留原文
- **常见技术缩写保留英文**：PvP/PvE、API、GUI、UI、FPS、MB/KB/GB、Mod、config 等——
  中文里夹杂这些是正常的，不算「中英混杂」
- **按键/快捷键名保留**：C 键、F 键、Ctrl、Shift、Alt 等——「按C键」「按F键」是正常译法
- 单个专有名词（如 Minecraft）、纯代码标识（类名/着色器变量/资源路径）无需翻译
- **换行符（\\n/实际换行）不是质量缺陷**：原文含换行、译文用 \\n 转义或保留换行都正常，
  绝不因「换行符」判不合格
- **占位符/格式符不是质量缺陷**：%s/%d/%% 等占位符只要保留即可，不必逐字符完全一致
- **空格/大小写不是质量缺陷**：专有名词（Xaero、Minecraft）前后有无空格、大小写差异
  都正常，绝不因「缺空格」「大小写」判不合格
- **内容完整可读即合格**：只要译文完整（无截断/漏翻）、意思正确、读得通，就算合格——
  换行符/占位符/空格/措辞微调都不影响合格
- **宁可不标记也不误伤**：不确定是否不合格时，默认「合格」跳过，绝不虚构问题"""


def _review_prompt(pairs: list[dict], target_lang: str, silly_mode: bool = False) -> str:
    """构造审查 prompt：逐行 [iN] 源 ||| 译，要求只输出不合格条目。"""
    lines = "\n".join(
        f"[i{i}] {p['source']} ||| {p['translated']}" for i, p in enumerate(pairs))
    # 胡言乱语模式：搞笑/热梗风格不算不合格，只拦「语义错误/漏翻/截断」——
    # 否则搞笑译文会被「审查重翻」闭环打回正常（用户诉求，F5）
    silly = ("\n（胡言乱语模式）译文以搞笑/玩梗/网络热梗风格表达不算不合格：只要语义仍忠实"
             "对应原文、无错译/漏翻/截断——幽默表达不是质量缺陷，只拦「语义错误、漏翻、截断」。"
             if silly_mode else "")
    return (f"你是 Minecraft 模组汉化质量审查员。逐条审查以下「源文本 → 译文」对（目标语言"
            f" {target_lang}），只标记**不合格**译文并给出原因。\n{_REVIEW_RULES}{silly}\n\n"
            f"输入每行：[i数字] 源文本 ||| 译文\n"
            f"输出：只输出不合格条目的 [i数字] 前缀 + 简短原因（≤15 字），一行一条。\n"
            f"若全部合格，输出：全部合格。\n不要输出其他任何内容。\n\n{lines}")


def parse_review(text: str, total: int) -> dict[int, str]:
    """解析审查输出：{索引: 原因}。整批合格/格式不符返回空（不误伤）。"""
    if _DONE_RE.match(text.strip()):
        return {}
    out: dict[int, str] = {}
    for line in text.splitlines():
        m = _PREFIX_RE.match(line)
        if not m:
            continue
        if m.group(4):                       # 「N. N、N)」数字编号（1-based）→ 0-based
            idx = int(m.group(4)) - 1
        else:                                # [iN] / *iN* / **iN**
            idx = next((int(g) for g in m.groups()[:3] if g is not None), -1)
        if 0 <= idx < total:
            reason = line[m.end():].strip().strip(":：-— ") or "译文质量不合格"
            # 清洗乱格式 reason：审查可能输出「源文本 ||| 译文」残段或「全部合格」等——
            # 取 ||| 后段；若清洗后仍是合格标记/空，丢弃该条（不误伤合理翻译）
            if "|||" in reason:
                reason = reason.split("|||")[-1].strip()
            reason = reason.strip(":：-— ")
            if not reason or _DONE_RE.match(reason):
                continue
            out[idx] = reason
    return out


async def _review_batch(engine, client, batch: list[dict], target_lang: str,
                        silly_mode: bool = False) -> dict[int, str]:
    """审查一批（≤_REVIEW_PAGE 条）：请求 LLM → 解析不合格索引。请求失败返回空（不误伤）。"""
    prompt = _review_prompt(batch, target_lang, silly_mode)
    body = {
        "model": engine.model,
        "messages": [
            {"role": "system", "content":
                "你是严格的 Minecraft 模组汉化质量审查员。你的职责是找出翻译不合格的条目，"
                "只输出不合格条目，绝不虚构问题，绝不放纵质量差的译文。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,      # 审查要稳定一致，低温
        "max_tokens": 4096,
    }
    # v1.2.9：RPM 令牌已在 review_translations.run_batch 的 sem 外取过，这里不重复
    # acquire——成功/429 的 report 信号仍喂回（共享配额撞了闸就该退档）
    _gate = getattr(engine, "rate_gate", None)
    try:
        resp = await client.post(f"{engine.base_url}/chat/completions", json=body)
        resp.raise_for_status()
        data = resp.json()
        out = data["choices"][0]["message"]["content"]
        if engine.on_usage:
            try:
                u = data.get("usage") or {}
                engine.on_usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
            except Exception:
                pass   # 修复：on_usage 回调异常不影响审查结果（否则整批审查被丢弃）
        if _gate is not None:
            _gate.report_ok()                     # 自动校准：审查成功也计入稳定信号
    except Exception as e:
        # v1.2.5：审查撞 429 也要喂校准信号（审查与翻译共享配额，撞了闸就该退档）
        if _gate is not None:
            _resp = getattr(e, "response", None)
            if _resp is not None and getattr(_resp, "status_code", None) == 429:
                _gate.report_ratelimit()
        return {}                # 审查请求失败：不误伤（宁可不审，不把好译文当坏的）
    return parse_review(out or "", len(batch))


async def review_translations(engine, pairs: list[dict], target_lang: str,
                              on_batch_start: Callable[[int], None] | None = None,
                              on_batch_done: Callable[[int], None] | None = None,
                              silly_mode: bool = False) -> list[dict]:
    """AI 批量审查译文质量，返回不合格条目列表 [{key, source, translated, reason}]。

    pairs: [{key, source, translated}] 待审条目（顺序无要求，key 原样返回）。
    分页并发（_REVIEW_PAGE 条/批）；批回调供调用方推进进度（审查请求期间前端
    进度/明细不静止）。请求失败/解析失败默认「合格」——审查是加分项，不能因
    审查自身故障把好译文当坏的误伤。
    """
    if not pairs:
        return []
    client = engine._get_client()
    # 统一吞吐：审查批次**跟随前端/动态测试设置的批大小**（engine.batch_size，受 TPM 约束）
    # ——用户要求「跟着 tpm 设置的批一起变」，不硬编码。上限 40 防审查输出截断（审查
    # prompt 是「N 条 源 ||| 译」翻倍，输出是不合格列表）。批大请求数少、省 RPM；慢 API
    # 下由并发（min_cap ≥ 并发）覆盖单批耗时，不再靠缩小批。
    _page = max(_REVIEW_PAGE, min(int(getattr(engine, "batch_size", 0) or _REVIEW_PAGE), 40))
    batches = [pairs[k:k + _page] for k in range(0, len(pairs), _page)]
    # v1.2.8：审查与翻译/硬编码判断共享**同一全局并发池**（engine._conc_sem）——
    # 任意时刻在途请求 ≤ 翻译档位并发，阶段串行下审查独占跑满该并发，不叠加。
    # 懒提升：无池时按引擎并发新建并写回共享（asyncio 单线程，赋值原子）。
    _conc_sem = getattr(engine, "_conc_sem", None)
    if _conc_sem is None:
        _conc_sem = asyncio.Semaphore(max(1, int(getattr(engine, "concurrency", _REVIEW_CONCURRENCY))))
        if hasattr(engine, "_conc_sem"):
            engine._conc_sem = _conc_sem
    sem = _conc_sem

    async def run_batch(batch: list[dict]) -> dict[int, str]:
        # v1.2.9：RPM 令牌**先取、sem 后取**（对齐翻译 run_chunk）——等待令牌的审查批
        # 不占全局并发池，翻译与审查并行（_dual_pipeline）时审查不被翻译的 chunk 挤死
        _gate = getattr(engine, "rate_gate", None)
        if _gate is not None:
            await _gate.acquire()
        async with sem:
            if on_batch_start:
                on_batch_start(len(batch))
            try:
                return await _review_batch(engine, client, batch, target_lang, silly_mode)
            finally:
                if on_batch_done:
                    on_batch_done(len(batch))

    results = await asyncio.gather(*(run_batch(b) for b in batches))
    bad: list[dict] = []
    for batch, res in zip(batches, results):
        for idx, reason in res.items():
            p = batch[idx]
            bad.append({"key": p["key"], "source": p["source"],
                        "translated": p["translated"], "reason": reason})
    return bad
