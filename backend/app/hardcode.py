# -*- coding: utf-8 -*-
"""M5-1 硬编码字节码扫描与替换核心。

汉化 mod 里硬编码在 JVM 字节码（.class）常量池中的字符串字面量。
使用 jawa（MIT 协议，2.2.0）解析/修改 class 文件，不再自研解析器。

jawa 类名加载方式（已实测，Windows）：
  - 单层类（无包名）：ClassLoader(work)["HelloMod"] 点化式即可。
  - 嵌套包类：ClassLoader 内部 path_map 的 key 是 os.path.relpath 生成的原生
    分隔符路径（Windows 下为反斜杠，如 com\\example\\Mod.class），直接传斜杠式
    "com/example/Mod" 会 FileNotFoundError。因此本模块在构造 ClassLoader 后把
    path_map 的 key 统一规范化为 POSIX 斜杠式，再以相对路径去 .class 后缀加载。
"""

import asyncio
import io
import json
import logging
import re
import shutil
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

from jawa.classloader import ClassLoader
from jawa.constants import String

from app.translate.common import should_translate

# 重新打包时跳过的 JAR 签名文件：改过字节码后签名必然失效，留着会让 JVM 拒载
_SIGNATURE_SUFFIXES = (".SF", ".RSA", ".DSA", ".EC")

# 类路径片段（com.example.Mod 的每个 "." 分隔段）
_CLASS_PATH_PART_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")

# JVM 方法描述符（参考 MIT 工具的 technicalPatterns）：
#   (Ljava/lang/String;)V 这类含类引用的签名旧正则 ^\([BCDFIJSZ\[L;]*\)...$ 会漏网
#   （类名里有 / 与小写字母），这里按 JVM 规范完整描述：
#   基础类型 [BCDFIJSZ]、类引用 Lcom/foo;、数组前缀 \[*；返回类型可为 V 或同类型。
_JVM_BASE_TYPE = r"(?:[BCDFIJSZ]|L[A-Za-z0-9/$_]+;)"
_JVM_TYPE_RE = r"(?:\[*" + _JVM_BASE_TYPE + r")"
_JVM_METHOD_DESC_RE = re.compile(r"^\((?:%s)*\)(?:%s|V)?$" % (_JVM_TYPE_RE, _JVM_TYPE_RE))


def is_hardcode_translatable(text: str) -> bool:
    """判断一段字节码字符串字面量是否值得硬编码汉化（候选）。

    参考 Minecraft-mod-translator（MIT License，版权归 饩雨 xiyu 2025）的
    isUserVisibleString/isTranslatableString 过滤思路：只排除明确的技术性
    标识符（包名/方法签名/描述符/常量名/纯数字/十六进制/分隔符/字面量等），
    **单词保留为候选**——"stone"/"parent" 这类是否翻译交给用户选择环节把关，
    不在此一刀切，避免漏翻大量单次词 UI 文本（Settings/Inventory 等）。
    """
    t = text.strip()
    if not (2 <= len(t) <= 100):
        return False
    # 含字母（拉丁/CJK/假名）才有意义，纯符号/数字串跳过
    if not re.search(r"[a-zA-Z一-鿿぀-ヿ]", t):
        return False
    # 技术性标识符排除（参考 MIT 工具的 technicalPatterns）
    if re.match(r"^[a-z]+(\.[a-z]+)+$", t):        # 包名 com.example
        return False
    if _JVM_METHOD_DESC_RE.match(t):      # 方法签名 (Ljava/lang/String;)V
        return False
    if re.match(r"^L[a-zA-Z0-9/$_]+;$", t):        # 类描述符 Lcom/Foo;
        return False
    if re.match(r"^\[[BCDFIJSZ\[L]", t):           # 数组描述符 [Ljava/lang/String;
        return False
    if re.match(r"^[A-Z_][A-Z0-9_]*$", t):         # 常量名 HELLO_WORLD
        return False
    if re.match(r"^(get|set|is)[A-Z]", t):         # getter/setter 方法名
        return False
    if re.match(r"^<(init|clinit)>$", t):          # 构造函数
        return False
    if re.match(r"^\d+(\.\d+)*$", t):              # 纯数字
        return False
    if re.match(r"^[a-f0-9]{8,}$", t, re.I):       # 十六进制
        return False
    if re.match(r"^[\\/.\\-_]+$", t):              # 分隔符
        return False
    if re.match(r"^(true|false|null)$", t, re.I):  # 字面量
        return False
    # modid:item（冒号且无空格）
    if ":" in t and " " not in t:
        return False
    # 类路径（每段都是合法 Java 标识符）com.example.Mod
    if "." in t and all(_CLASS_PATH_PART_RE.fullmatch(s) for s in t.split(".")):
        return False
    # ---- 粗过滤（voxy 实测：655 条硬编码候选绝大多数是技术串，砍到几十条真实候选）----
    # 数据串/代码特征：分号/竖线分隔数据、花括号模板、shader 指令（#version）、
    # 代码下标/函数调用（printfOutputStruct.stream[..] / uint( / vec2(）→ 排除
    if any(ch in t for ch in ";|{}#[]()"):
        return False
    if "printf" in t:
        return False
    # 资源/文件路径（无空格的多为路径拼接，textures/atlas/blocks.png 等）→ 排除
    if "/" in t or "\\" in t:
        return False
    # 纯小写单词（≤16 字符、无空格）：voxy/id/path/minecraft/bobby 等标识符 → 排除。
    # 含空格的真实 UI 句子（Could not parse config）与 %s/%d 占位符不受影响，
    # 前者留 ai_judge 判断是否日志。
    if re.match(r"^[a-z]{2,16}$", t):
        return False
    return True


def _extract_jar(jar: Path, work: Path) -> None:
    """把 jar 解压到 work 目录（已存在先清空）。

    zip-slip 防护：对 zip 条目名用 PurePosixPath 规范化，含 `..` 段、
    绝对路径、或解析后逃逸出 work 的条目一律跳过（不入盘），
    不整体拒绝 jar，保证扫描/替换不因单个恶意条目中断。
    """
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    work_resolved = work.resolve()
    with zipfile.ZipFile(jar, "r") as zf:
        for name in zf.namelist():
            # 规范化条目名：拒绝 ../ 段与绝对路径
            clean = PurePosixPath(name)
            if clean.is_absolute() or ".." in clean.parts:
                continue
            target = work.joinpath(*clean.parts)
            try:
                # 双保险：解析后必须仍在 work 内（防符号链接/规范化逃逸）
                target.resolve().relative_to(work_resolved)
            except ValueError:
                continue
            if name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _class_loader(work: Path) -> ClassLoader:
    """构造 ClassLoader，并把 path_map 键规范化为 POSIX 斜杠式（跨平台）。"""
    loader = ClassLoader(str(work))
    loader.path_map = {
        key.replace("\\", "/"): value for key, value in loader.path_map.items()
    }
    return loader


def _class_name(p: Path, work: Path) -> str:
    """从 class 文件路径换算 jawa 加载用的类名（POSIX 斜杠式，去 .class）。"""
    return p.relative_to(work).as_posix()[: -len(".class")]


def _repack(work: Path, jar: Path) -> None:
    """把 work 目录重新打包为 zip，覆盖 jar 路径（调用方保证 jar 是副本）。"""
    with zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(work.rglob("*")):
            if p.is_dir():
                continue
            rel = p.relative_to(work).as_posix()
            # 跳过 META-INF 下签名文件，避免字节码变更后签名失效
            if rel.endswith(_SIGNATURE_SUFFIXES) and "/META-INF/" in f"/{rel}":
                continue
            zf.write(p, rel)


def _trim_context(raw: set[str], max_items: int = 10, max_chars: int = 60) -> list[str]:
    """context 去重截断：保持出现顺序，最多 max_items 条，每条最多 max_chars 字符。

    默认 10 条/60 字符（曾为 30/80）：ai_judge 逐条判断时 prompt 更小，配合
    分页并发，显著降低 655 条候选时的 LLM 卡慢（voxy 实测）。
    """
    seen: set[str] = set()
    out: list[str] = []
    for s in raw:
        s2 = s[:max_chars]
        if s2 in seen:
            continue
        seen.add(s2)
        out.append(s2)
        if len(out) >= max_items:
            break
    return out


def scan_hardcoded_candidates(jar: Path) -> list[dict]:
    """扫描硬编码候选：返回 [{"text", "occurrences", "context"}]，按出现频率降序。

    context = 同一 class 内相邻的字符串常量（排除自身，去重，最多前 30 条、
    每条 ≤80 字符），供 AI 判断「该字符串是否用户可见文本」
    （方块译匠 inspect_class_context 思路）。
    """
    work = jar.parent / f".{jar.stem}_hw"
    counts: Counter[str] = Counter()
    contexts: dict[str, set[str]] = {}
    try:
        _extract_jar(jar, work)
        loader = _class_loader(work)
        for p in sorted(work.rglob("*.class")):
            name = _class_name(p, work)
            try:
                klass = loader[name]
            except Exception:
                continue  # 单个 class 损坏/不可加载：跳过，不拖垮整包扫描
            class_strings = [
                c.string.value
                for c in klass.constants
                if isinstance(c, String)
            ]
            for c in klass.constants:
                if isinstance(c, String):
                    t = c.string.value
                    if is_hardcode_translatable(t):
                        counts[t] += 1
                        # context：同 class 的其他 String 常量（原始、不过滤，供 AI 判断语境）
                        contexts.setdefault(t, set()).update(s for s in class_strings if s != t)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return [
        {"text": t, "occurrences": n, "context": _trim_context(contexts.get(t, set()))}
        for t, n in counts.most_common()
    ]


def scan_hardcoded_strings(jar: Path) -> list[str]:
    """扫描 jar 内硬编码的可翻译字符串（去重排序，兼容旧调用方）。

    复用 scan_hardcoded_candidates 的频率扫描结果，只取 text 排序返回。
    """
    return sorted(c["text"] for c in scan_hardcoded_candidates(jar))


def replace_hardcoded_strings(jar: Path, mapping: dict[str, str]) -> dict:
    """替换 jar 内字节码中的硬编码字符串。

    逐 class 修改命中 mapping 的 String 字面量；每个 class 改后重读校验
    （能解析且 String 数不变）才认成功，失败记入 failed_classes 跳过，
    不中断整体。最后重新打包覆盖原 jar。

    返回 {"replaced": int, "failed_classes": list[str], "skipped": int}
      - replaced: 成功替换的 String 字面量总数
      - failed_classes: 加载/修改/校验失败的 class 名列表
      - skipped: 解压到的 class 总数（含未命中 mapping 的）
    """
    work = jar.parent / f".{jar.stem}_hw"
    replaced = 0
    skipped = 0
    failed_classes: list[str] = []
    try:
        _extract_jar(jar, work)
        loader = _class_loader(work)
        class_files = sorted(work.rglob("*.class"))
        skipped = len(class_files)
        for p in class_files:
            name = _class_name(p, work)
            try:
                klass = loader[name]
                before = [
                    c.string.value
                    for c in klass.constants
                    if isinstance(c, String)
                ]
                changed = 0
                # M5-recheck：先记录每个被替换 String 的期望内容，供保存后内容级校验。
                # 必须在修改前收集——修改后值已变成译文，无法再反查 mapping。
                expected_counts: Counter[str] = Counter()
                for c in klass.constants:
                    if isinstance(c, String) and c.string.value in mapping:
                        expected_counts[mapping[c.string.value]] += 1
                        c.string.value = mapping[c.string.value]
                        changed += 1
                if changed:
                    # save 前保留原字节：校验失败时写回还原，
                    # 确保 failed class 不改坏字节进输出 jar
                    original_bytes = p.read_bytes()
                    try:
                        # 先写入内存，save 本身失败不截断原文件
                        buf = io.BytesIO()
                        klass.save(buf)
                        p.write_bytes(buf.getvalue())
                        # 重读校验：能解析、String 数不变、且每个期望内容都真实写入才算成功。
                        # jawa 的 Modified-UTF8 编码对 emoji（U+10000+）与边界码位
                        # （0x7FF/0x800/0xFFFF）会静默丢弃，String 数不变但内容已损坏，
                        # 因此必须逐值断言，不能只比数量。
                        verify = _class_loader(work)[name]
                        after = [
                            c.string.value
                            for c in verify.constants
                            if isinstance(c, String)
                        ]
                        if len(after) != len(before):
                            raise ValueError(
                                f"{name}: 替换后 String 数 {len(after)} != 替换前 {len(before)}"
                            )
                        after_counts = Counter(after)
                        for expect, cnt in expected_counts.items():
                            if after_counts[expect] < cnt:
                                raise ValueError(
                                    f"{name}: 替换内容 {expect!r} 写入后丢失"
                                    f"（Modified-UTF8 编码不支持 emoji 等码位？）"
                                )
                        replaced += changed
                    except Exception:
                        p.write_bytes(original_bytes)  # 还原为原始字节
                        raise
            except Exception as exc:
                failed_classes.append(f"{name} ({type(exc).__name__}: {exc})")
        _repack(work, jar)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {
        "replaced": replaced,
        "failed_classes": failed_classes,
        "skipped": skipped,
    }


# ---------- B 阶段：硬编码 AI 自动判断 + 翻译（方块译匠 scan_class_text 思路） ----------

# 每批发送给 LLM 判断的候选上限
_AI_JUDGE_PAGE = 25

# ai_judge 并发限流：与 LLMClient.translate_batch 的默认并发（concurrency=5）对齐。
# 分页串行在候选多（voxy 实测 655 条）时逐批等待，是卡慢主因之一；
# 并发 5 页在保持供应商请求速率可控的前提下把多批请求并行发出。
_AI_JUDGE_CONCURRENCY = 5

def _ai_judge_system_prompt(target_lang: str) -> str:
    """system 提示词：判断「是否用户可见文本」并翻译成 target_lang 对应语言。

    不再写死简体中文——zh_tw 时提示繁体中文，避免繁体目标产出简体（B 审查 🟡2）。
    """
    return (
        "你是 Minecraft 模组汉化助手。判断每段字符串是否是「玩家在游戏中能直接看到的文本」"
        "（GUI 标题、物品/工具提示、聊天消息、成就名等）。技术标识符（JSON 键、资源路径、"
        "注册 ID、方法签名、包名）→ translatable=false。是用户可见文本 → translatable=true "
        f"并翻译成 {target_lang} 对应语言（如 zh_cn 为简体中文、zh_tw 为繁体中文），"
        "保留 %s %d 等占位符。严格输出 JSON 数组，不要任何解释或 Markdown："
        '[{"text": "...", "translatable": true, "translation": "..."},'
        ' {"text": "...", "translatable": false, "translation": ""}]'
    )


def _parse_ai_judge_response(content: str) -> list[dict] | None:
    """解析 LLM 输出的 JSON 数组；非法 JSON / 顶层不是数组 → None（该批跳过）。

    容错：剥 Markdown 代码块围栏；容忍顶层为 {"results": [...]} 或 {"items": [...]}
    对象包装（兼容 response_format json_object 约束下的对象输出形态）。
    """
    s = content.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    try:
        data = json.loads(s)
    except (ValueError, json.JSONDecodeError):
        return None
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("results", "items"):
            v = data.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return None


def _ai_judge_item_result(items: list[dict], cand: dict) -> dict[str, str] | None:
    """从解析出的条目里取与 cand.text 匹配的结果。

    返回 {text: translation}（translatable=true 且带译文）、{}（判定不可见）、
    或 None（未匹配到该候选）。
    """
    for item in items:
        if item.get("text") == cand["text"]:
            if item.get("translatable") and item.get("translation"):
                return {cand["text"]: item["translation"]}
            return {}
    return None


async def _ai_judge_single(engine, client, cand: dict, target_lang: str) -> dict[str, str] | None:
    """ai_judge 单条降级：对单个候选单独发请求判断并翻译（P0 根因 3）。

    成功返回 {text: translation} 或 {}（判定不可见），失败/未匹配返回 None。
    """
    body = {
        "model": engine.model,
        "messages": [
            {"role": "system", "content": _ai_judge_system_prompt(target_lang)},
            {"role": "user", "content": json.dumps(
                [{"text": cand["text"], "context": cand.get("context") or []}],
                ensure_ascii=False)},
        ],
        "temperature": 0.2,
    }
    try:
        resp = await client.post(f"{engine.base_url}/chat/completions", json=body)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        if engine.on_usage:
            u = data.get("usage") or {}
            engine.on_usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
        if not content:
            return None
    except Exception as exc:
        logger.warning("ai_judge 单条降级请求失败 %s：%s", cand["text"], exc)
        return None
    items = _parse_ai_judge_response(content)
    if not items:
        # 兼容单对象输出：{"text": ..., "translatable": ..., "translation": ...}
        try:
            obj = json.loads(content.strip().strip("`"))
            if isinstance(obj, dict):
                items = [obj]
        except (ValueError, json.JSONDecodeError):
            items = []
    if not items:
        return None
    return _ai_judge_item_result(items, cand)


async def _ai_judge_batch(engine, client, batch: list[dict], target_lang: str) -> dict[str, str]:
    """对一批候选发一次 LLM 请求并解析，返回该批 translatable=true 的 {text: translation}。

    提取自原 ai_judge_translate 的循环体，供并发调度复用（每批一个任务）。
    容错语义与原实现一致：请求失败/空内容 → 整批跳过；非法 JSON/空数组 →
    逐条降级 _ai_judge_single，不整批丢（P0 根因 3）。
    """
    result: dict[str, str] = {}
    payload = [
        {"text": c["text"], "context": c.get("context") or []}
        for c in batch
    ]
    body = {
        "model": engine.model,
        "messages": [
            {"role": "system", "content": _ai_judge_system_prompt(target_lang)},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "temperature": 0.2,
    }
    try:
        resp = await client.post(f"{engine.base_url}/chat/completions", json=body)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        if engine.on_usage:
            u = data.get("usage") or {}
            engine.on_usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
        if not content:
            # content 为 null（部分供应商流式/拒绝场景）：按整批跳过，
            # 若交给 _parse 会在 content.strip() 抛 AttributeError（B 审查 🟡1）
            logger.warning("ai_judge 返回空内容，跳过 %d 条", len(batch))
            return result
    except Exception as exc:
        # 请求失败（网络/API/HTTP 错误）→ 整批跳过，不中断其他批次
        logger.warning("ai_judge 批次请求失败，跳过 %d 条：%s", len(batch), exc)
        return result
    items = _parse_ai_judge_response(content)
    if not items:
        # 非法 JSON / 空数组 → 不整批丢，对该批候选逐条降级（P0 根因 3）
        logger.warning("ai_judge 输出非法 JSON/空数组，%d 条逐条降级", len(batch))
        for cand in batch:
            single = await _ai_judge_single(engine, client, cand, target_lang)
            if single is None:
                logger.warning("ai_judge 单条降级失败：%s", cand["text"])
                continue
            result.update(single)
        return result
    for item in items:
        text = item.get("text")
        if not text:
            continue
        # 仅接受显式 translatable=true 且带非空 translation 的条目
        if item.get("translatable") and item.get("translation"):
            result[text] = item["translation"]
    return result


async def ai_judge_translate(engine, candidates: list[dict], target_lang: str) -> dict[str, str]:
    """LLM 判断硬编码候选是否用户可见并翻译。

    分页 ≤25 条/批；每批把 [{text, context}] 发 LLM，要求严格 JSON 数组输出：
    [{"text": "...", "translatable": true/false, "translation": "..."}]
    只返回 translatable=true 的 {text: translation}。
    解析容错：非法 JSON / 缺失字段 → 该批跳过，logger.warning 记录。
    复用 engine（LLMClient）的 base_url/model 与 httpx 客户端发 /chat/completions。

    提速：分页串行改为 asyncio.gather + Semaphore(_AI_JUDGE_CONCURRENCY) 并发处理各页，
    与 LLMClient.translate_batch 的并发模式对齐（voxy 实测 655 条候选时分页串行卡慢）。
    """
    if not candidates:
        return {}
    client = engine._get_client()  # LLMClient 内部复用的 httpx.AsyncClient
    batches = [
        candidates[k:k + _AI_JUDGE_PAGE]
        for k in range(0, len(candidates), _AI_JUDGE_PAGE)
    ]
    sem = asyncio.Semaphore(_AI_JUDGE_CONCURRENCY)

    async def run_batch(batch: list[dict]) -> dict[str, str]:
        async with sem:
            return await _ai_judge_batch(engine, client, batch, target_lang)

    results = await asyncio.gather(*(run_batch(b) for b in batches))
    merged: dict[str, str] = {}
    for r in results:
        merged.update(r)
    return merged
