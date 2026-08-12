import re
from dataclasses import dataclass

from app.detect import needs_lang_value_translation
from app.scanner import ModScan

# 程序内部标识保护：URL / 绝对路径 / 相对资源路径（含扩展名）/ 注册 ID（modid:key）
# 形态是程序内部标识，翻译会破坏资源位置/Identifier（用户实测 Xaero 用翻译名拼
# Identifier 崩溃）→ 保护不翻。
#
# 收紧要点（Xaero 审查修复）：原「/token + 空格词组」会吞英文句子，导致
# world/server. You can read... 这类普通文本被整条判为程序标识而不进缺口（缺键）。
# 现只保护：绝对路径（≥2 段 /）、相对路径含扩展名、注册 ID。
_PROTECTED_RE = re.compile(
    r"^(?:https?://\S+"
    r"|(?:/[A-Za-z0-9_.@-]+){2,}"
    r"|(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[a-z0-9]+"
    r"|[A-Za-z0-9_.-]+:[A-Za-z0-9_./-]+)$")


# 音乐唱片/署名键（music_disc/soundtrack/credit/author/artist）值形如「xxx - yyy」→ 保护不翻
# （翻译会破坏「曲名 - 作者」结构）
_CREDITED_KEY_RE = re.compile(r"(?:music_disc|soundtrack|credit|author|artist)", re.I)
_CREDITED_VALUE_RE = re.compile(r"^.{2,80}\s[-—–]\s.{2,120}$", re.S)


def compute_gaps(source: dict[str, str], existing: dict[str, str],
                 target_lang: str = "zh_cn") -> list[str]:
    """返回需翻译的 key：缺失、空值、或已有值仍是源语言（未翻译占位）。

    已有值的判定（修复：原版自带 zh_cn 值却为英文时缺口计算漏掉，导致英文残留）：
      - zh_cn/zh_tw 目标：已有值含中文（占比 >40%）视为已汉化跳过，否则补翻
        （与翻译循环 needs_lang_value_translation 同规则）；
      - 其他目标（拉丁/日韩等）：已有值仍等于源文本视为未翻译占位（补翻），
        否则视为已翻译跳过。
    """
    out: list[str] = []
    for k, v in source.items():
        # effect.* 状态效果名：可能被任意 mod 拼进资源位置 Identifier，翻译成中文会产生
        # 非法字符崩溃（Xaero 多次实测）。**必须强制重新处理**（语言文件阶段写入英文覆盖）——
        # 若 zh_cn 已有中文值，compute_gaps 会判「已汉化」而漏掉，旧中文 effect 残留导致崩。
        if k.startswith("effect."):
            out.append(k)
            continue
        # 保护 URL/路径/注册 ID：程序标识翻译会破坏资源位置 → 不翻
        if _PROTECTED_RE.match(v):
            continue
        # 音乐/署名（xxx - yyy）→ 保护不翻
        if _CREDITED_KEY_RE.search(k) and _CREDITED_VALUE_RE.match(v):
            continue
        if k not in existing or not existing[k].strip():
            out.append(k)
            continue
        ev = existing[k]
        if target_lang in ("zh_cn", "zh_tw"):
            if needs_lang_value_translation(ev, target_lang):
                out.append(k)          # 已有值仍是英文/未汉化 → 补翻（Sodium 场景）
        elif ev == v:
            out.append(k)              # 拉丁目标：值还是源串 → 未翻 → 补翻
    return out


@dataclass
class TranslationJob:
    modid: str
    key: str
    source_text: str


def build_jobs(scans: list[ModScan], target_lang: str = "zh_cn") -> list[TranslationJob]:
    """把所有 mod 的翻译缺口汇总成作业列表（含已有值仍是源语言的占位 key）。

    修复：同 modid 同 key 去重（同一 modid 可能多个语言文件/多个 jar 有相同 key，
    不去重会产生重复 job → 产物互相覆盖、进度多计）。
    """
    jobs: list[TranslationJob] = []
    seen: set[tuple[str, str]] = set()
    for scan in scans:
        for key in compute_gaps(scan.source_entries, scan.target_entries, target_lang):
            dedup = (scan.modid, key)
            if dedup in seen:
                continue
            seen.add(dedup)
            jobs.append(TranslationJob(scan.modid, key, scan.source_entries[key]))
    return jobs
