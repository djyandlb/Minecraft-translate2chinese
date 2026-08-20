# -*- coding: utf-8 -*-
"""A5 统一全自动翻译流程（B 阶段：全文本覆盖 + 硬编码 AI 自动判断）。

一个入口跑完全部：拖入整合包 / mod jar / 地图 → 自动识别 → 全文本覆盖
（语言文件 + 结构化 JSON + en_us 文本）+ 硬编码 AI 自动判断一起翻译
（共用引擎/记忆/状态机）→ 产物 = 资源包 zip + 汉化 jar 副本。
map 委托 maps_flow。原 jar/存档只读，一切写操作只在 work 副本。

引擎分流：
  - LLMClient：硬编码候选走 ai_judge_translate（LLM 判断是否用户可见并翻译）
  - MachineClient：无法 AI 判断，跳过硬编码（明确 warn），json/lines 文本覆盖照做
  - 其他兜底引擎（测试假引擎等）：硬编码逐条全翻（无 AI 判断）

流程化（P2-1 拆分）：run_auto_translation 编排入口 → AutoFlow 类按阶段
（lang / json+pack / hardcode / build）拆分，共享状态存 self，方法独立可读，
替代原 970 行巨型函数的闭包堆叠。
"""
import asyncio
import json
import random
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath

# v1.4.5：重试参数常量（业界最佳实践：指数退避+抖动，区分错误类型）
_RETRY_PARAMS = {
    "ratelimit": {"base": 10.0, "max": 60.0},   # 限流：等更久
    "timeout": {"base": 2.0, "max": 30.0},      # 超时：等短一些
    "network": {"base": 5.0, "max": 30.0},      # 网络错误：中等
    "server": {"base": 3.0, "max": 30.0},       # 服务器错误：中等
    "other": {"base": 5.0, "max": 30.0},        # 其他：中等
}
_RETRY_MAX_TOTAL_TIME = 90.0  # 重试总超时（秒）

from app.archive import (archive_fingerprint, dir_fingerprint, extract_cached,
                         is_archive)
from app.audit import audit_invariants, audit_translation
from app.cleanup import cleanup_task_work
from app.config import AppConfig
from app.cfpa import (bundled_i18n_jar, download_cfpa, list_bundled_versions,
                      load_bundled_cfpa, load_cfpa, match_zip_name)
from app.detect import (_HARDCODE_MAX_BYTES, detect_input_type, detect_mc_version,
                        detect_source_lang, infer_pack_format, needs_lang_value_translation,
                        needs_translation, unwrap_bare_wrapper)
from app.diff import build_jobs
from app.langfile import lang_value_ok, parse_properties, write_properties
from app.glossary import load_glossary, strip_particle, term_inject_prompt
from app.hardcode import (ai_judge_translate, replace_hardcoded_strings,
                          scan_hardcoded_candidates)
from app.maps.flow import run_map_translation
from app.memory import MemoryStore, extract_terms
from app.placeholder import clean_surrogates, validate as validate_placeholders
from app.models import AutoRequest, MapTranslateRequest
from app.modname import friendly_output_name
from app.safeerr import sanitize_error
from app.version import version_to_pack_format, pack_format_spec
from app.resourcepack import build_resource_pack, build_resource_pack_dir
from app.review import review_translations
from app.scanner import scan_jar
from app.tasks import TaskStore
from app.text_sources import (TextSource, discover_pack_text_sources,
                              discover_text_sources, render_jar_source,
                              render_jar_sources_batch, render_pack_source,
                              write_lang_into_jar, write_translated)
from app.translate.engine import create_engine
from app.verify import verify_translated_jar
from app.vp import bundled_vp_jar, build_vp_module, download_vault_patcher, infer_modpack_runtime
from app.translate.han import is_same_script, simplify, traditional
from app.translate.llm import LLMClient
from app.translate.machine import MachineClient

# 汉化命名映射：target_lang → 显示名（zh_cn→简体中文、zh_tw→繁体中文、ja_jp→日文……）。
# 对齐 modname._LANG_DISPLAY：材质包描述/产物名「{语言}化」跟随目标语言，不只是中文。
_LANG_NAMES = {
    "zh_cn": "简体中文", "zh_tw": "繁体中文",
    "ja_jp": "日文", "ko_kr": "韩文",
    "fr_fr": "法文", "de_de": "德文", "es_es": "西班牙文", "es_mx": "西班牙文",
    "es_ar": "西班牙文", "ru_ru": "俄文", "it_it": "意大利文", "pt_br": "葡萄牙文",
    "pt_pt": "葡萄牙文", "vi_vn": "越南文", "th_th": "泰文", "id_id": "印度尼西亚文",
    "uk_ua": "乌克兰文", "pl_pl": "波兰文", "tr_tr": "土耳其文", "nl_nl": "荷兰文",
}

# 审查 reason 中「合理保留原文」的判定词（命中 → 漏翻不算失败，AI 判定保留正确）：
# 专有名词/模组名/命令/代码标识/资源路径/Identifier/变量名/类名/API/注册/路径等。
# 用于终审残余精准判定——不能把「该翻没翻」草率放过，也不能把「合理保留」误报失败。
# 修复：收紧合理保留判定——只认**明确技术类别**（专有名词/命令/代码标识/路径/Identifier 等），
# 去掉「保留原文/不需要翻译/不该翻/保持原文」等行为描述词：AI 措辞「译文保留原文未翻译，
# 这是用户可见文本应翻译」若命中行为词会被误判合理保留，把该翻没翻的放行（Agent 审查确认）
# 修复（recheck）：审查 reason 的「合理保留」判定词收窄——原表含「渲染」「着色器」
# 「技术名词」「按键」「快捷键」「格式串/格式符」等 **UI 语义词汇**（Render Distance 该翻
# 成「渲染距离」、Key Bindings 该翻「按键绑定」），AI reason 一沾这些词就把该翻的界面
# 文本放行成纯英文（用户实测「审查算过了但纯英文残留」根因之一）。纯占位符/格式码由
# _is_legit_keep_by_source 按原文规则兜底，不靠 reason 词汇。保留词只收**确定性技术类别**。
_LEGIT_KEEP_RE = re.compile(
    r"专有名词|模组名|命令|代码标识|资源路径|Identifier|变量名|类名|API名|"
    r"玩家名|注册(?:名|ID)|modid|注册ID|本地化键|配方ID", re.I)

# AI 语境归一化判定 system prompt（v1.1.0 重构）：
# 归一化只针对「专有名词 + 语境相同」，常用词（light/right/iron）绝不机械统一。
_NORM_JUDGE_SYSTEM = """你是 Minecraft 汉化的「术语归一化判定员」。对每组「同一英文原文出现了多个不同译文」判断：
1) 该原文是否是**专有名词/特有名词**（物品名/人名/模组名/术语）？light/right/iron 等常用词不是；
2) 各出现的语境是否**相同**（指同一事物/同一语义）？
3) 只有「是专有名词 且 语境相同」才应统一；语境不同（如 right 在「右键/正确/权利」）绝不统一。
对每组输出一行：[i编号] 统一 规范译名   或   [i编号] 不统一
示例：
[i0] 统一 泽诺
[i1] 不统一
只输出判定行，不要解释。"""


def _is_legit_keep(reason: str) -> bool:
    """审查 reason 判定为「合理保留原文」（专有名词/命令/代码标识/路径等）→ 不算漏翻失败。"""
    return bool(_LEGIT_KEEP_RE.search(reason or ""))


# 合法罗马数字文法（千/百/十/个位组合，1-3999）：贪心数值算法会把 DVD 判成 D+V+D=995，
# 故用文法严格匹配（D V 不能相邻）。
_ROMAN_RE = re.compile(
    r"^(?:M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3}))$")


def _is_roman(s: str) -> bool:
    """验证是否合法罗马数字（1-3999）。修复（Agent 审查）：原字符集校验 `[IVXLCDM]{1,6}`
    把 DVD/CIVIL/LCD 等由 I/V/X/L/C/D/M 组成的真实英文词误判「合理保留」→ 永不翻译。"""
    return bool(s) and bool(_ROMAN_RE.match(s))


def _is_proper_noun(text: str) -> bool:
    """形态启发判断「是否可能是专有名词/特有名词」（AI 语境归一化的**候选筛选**）。

    用户核心诉求：归一化只针对特有名词（Zeno、modid、物品名），**常用词（light/right/
    iron）绝不归一化**——right 翻成「右面」后所有 right 被硬替换成「右面」、light 翻成
    「灯」后 light blue 变「灯蓝色」的机械归一化灾难。本函数把「形态上像专名」的
    （首字母大写/驼峰/命名式）筛选进候选，小写常用词天然返回 False 被挡在门外。

    注意：这是**宽松候选筛选**（宁多勿漏，句首大写单词也会进），最终语境是否相同、
    是否真专名由 AI 判定（_ai_judge_normalization）把关——AI 说不同语境就不统一。
    """
    s = (text or "").strip()
    if not s:
        return False
    if len(s) < 2 or not re.search(r"[A-Za-z]", s):
        return False                       # 空/单字母/纯数字/纯符号 → 非专名
    if "_" in s or "-" in s:
        return True                        # 命名式（No_Minimap、Craft-Table）→ 像标识符/专名
    if re.search(r"[A-Z]", s[1:]):
        return True                        # 驼峰/混合大小写（ZenoSword、ModelView）→ 像专名/代码标识
    if s[0].isupper():
        return True                        # 首字母大写（Zeno / Iron Ingot 整体）→ 像专名
    return False                           # 全小写词/短语（light/right/of the orb）→ 常用词，不归一化


def _is_key_combo(s: str) -> bool:
    """按键组合判定（v1.3.3，用户「ALT+S 被打回」）：ALT+S / CTRL+SHIFT+X / F5 / <None>
    是按键绑定不该翻译——审查判「不合格」重翻无意义且浪费 token。匹配即保留原文。"""
    t = s.strip()
    # v1.3.4（Agent recheck）：只保留 <None>（按键绑定的「无绑定」占位）——裸 "None"
    # 是 UI 选项文本（「无」）该翻译，不误判保留
    if t in ("<None>", "<none>"):
        return True
    # 大写键名 + 字母组合：ALT+S、CTRL+SHIFT+S、CMD+S（+ 连接）
    if re.fullmatch(r"(?:[A-Z][A-Z0-9]*\+)+[A-Z0-9]+", t):
        return True
    # F1-F12 功能键
    if re.fullmatch(r"F(?:1[0-2]|[1-9])", t):
        return True
    # 单键名
    if t in ("ESC", "TAB", "SPACE", "ENTER", "SHIFT", "CTRL", "ALT", "CMD",
             "COMMAND", "SUPER", "BACKSPACE", "DELETE", "INSERT", "HOME", "END",
             "PAGEUP", "PAGEDOWN"):
        return True
    return False


def _is_legit_keep_by_source(source: str) -> bool:
    """按**原文文本**判定「保留原文是正确决策」（审查前分流，防 AI 审查误杀）。

    用户反馈：翻译报告一堆「审查不通过：没有中文译文/译文丢失占位符」——这些大多是
    AI 翻译时**合理保留**的专有名词（Balm/Discord）、键位（Alt/Ctrl）、占位符格式串
    （%s/§e>§r）、代码格式说明。这里用规则预判「原文确实该保留」→ 不送 AI 审查。

    **修复（recheck）**：原规则过宽——括号（'Left (click)'）、单英文词（'Invite'/
    'Settings'）、全大写短语（'Block Reach'/'Raining/Snowing'）全被误判「合理保留」→
    该翻的界面文本被永久留英文（不进审查）。收窄为**只预保留确定翻不动的**：
    - 无英文字母（纯符号/数字/占位符，如 (%1$s): %2$s、§e>§r %s §e<§r）→ 保留
    - 无空格 + 点/冒号分隔的类名/域名/资源定位符（com.example.Mod、path:to）→ 保留
    其余（含实词的真文本，无论括号/大小写/单词）→ **交给 AI 审查判定**是否保留。
    """
    s = (source or "").strip()
    if not s:
        return True
    # 纯罗马数字（I/II/III/IV/V/VI/VII/VIII/IX/X…）→ 保留（用户诉求；_is_roman 校验合法序列）
    if _is_roman(s):
        return True
    # v1.3.3 修复（用户「ALT+S 被打回」）：按键组合（ALT+S / CTRL+SHIFT+X / F5 / <None>）
    # 是按键绑定不该翻译——审查判「不合格」强制重翻无意义且浪费 token。判合理保留。
    if _is_key_combo(s):
        return True
    # v1.3.4（用户「同批 10 条占位符格式全失败」）：占位符格式串（%s/%d/%0.3f/%1$d +
    # 短标签如 "Exp: %0.3f"）——AI 保留占位符是正确行为，审查判「非目标语言」强制重翻
    # 徒劳（重翻仍原文 → 记 failed）。短格式串（≤40 字符、无 ≥4 字母实词）判合理保留。
    # 长实词标签（"Damage: %s" 的 Damage）仍走翻译（该翻成「伤害：%s」）。
    if "%" in s and len(s) <= 40 and not re.search(r"[A-Za-z]{4,}", s):
        return True
    # 移除 %xx 占位符（%s/%d/%%/%1$s/%n 等）与 Minecraft § 颜色码（§e=黄/§r=重置，
    # 其字母是格式码非实词）后无英文字母 → 纯占位符/格式串（如 (%1$s): %2$s、
    # §e>§r %s §e<§r）→ 保留
    no_ph = re.sub(r"%[^A-Za-z]*[A-Za-z]?", "", s)
    no_ph = re.sub(r"§[0-9a-fk-or]", "", no_ph)
    if not re.search(r"[A-Za-z]", no_ph):
        return True
    # 代码标识形态 → 保留，但收窄（修复 recheck）：原规则「无空格 + 任一 . 或 :」过宽，
    # 界面标签「Config:」「Options:」「Difficulty:」（无空格但带尾冒号）被误判保留。
    # 要求确定性代码形态：
    #   冒号 → 资源定位符/语言键双段（两端代码字符，如 minecraft:diamond、zh_cn:title）
    #   点   → 类路径/域名/文件名（≥2 段点分隔，如 com.example.Mod、item.file.json）
    if not re.search(r"\s", s):
        if re.fullmatch(r"[A-Za-z0-9_.-]+:[A-Za-z0-9_./-]+", s):
            return True
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+", s):
            return True
    return False   # 其余送 AI 审查（AI 判定保留还是翻译，不代码预判）


def lang_display_name(target_lang: str) -> str:
    """汉化命名映射：zh_cn→简体中文、zh_tw→繁体中文，其他 target_lang 原样。"""
    return _LANG_NAMES.get(target_lang, target_lang)


def _shader_langcode(mc_lang: str) -> str:
    """MC 语言代码 → 光影语言文件 langcode（光影约定 en_US.lang 驼峰）。

    zh_cn→zh_CN、zh_tw→zh_TW、ja_jp→ja_JP；未知含 _ 的代码按「_ 后大写」；
    无 _ 的（如 enus）原样。光影包按此命名切语言才生效。
    """
    if "_" in mc_lang:
        a, b = mc_lang.split("_", 1)
        return f"{a.lower()}_{b.upper()}"
    return mc_lang.lower()


_MC_VER_RE = re.compile(r"(1\.\d{1,2}(?:\.\d{1,2})?)")


def _jar_mc_version(jar: Path) -> str:
    """从单个 jar 元数据读 MC 版本（fabric.mod.json / forge·neoforge mods.toml）。"""
    try:
        with zipfile.ZipFile(jar) as zf:
            names = zf.namelist()
            if "fabric.mod.json" in names:
                d = json.loads(zf.read("fabric.mod.json").decode("utf-8"))
                spec = str((d.get("depends") or {}).get("minecraft", ""))
                m = _MC_VER_RE.search(spec)
                if m:
                    return m.group(1)
            toml = "META-INF/neoforge.mods.toml" if "META-INF/neoforge.mods.toml" in names else ("META-INF/mods.toml" if "META-INF/mods.toml" in names else None)
            if toml:
                import tomllib
                data = tomllib.loads(zf.read(toml).decode("utf-8"))
                deps_root = data.get("dependencies")
                if isinstance(deps_root, dict):
                    for dep_list in deps_root.values():
                        if not isinstance(dep_list, list):
                            continue
                        for dep in dep_list:
                            if isinstance(dep, dict) and dep.get("modId") == "minecraft":
                                m = _MC_VER_RE.search(str(dep.get("versionRange", "")))
                                if m:
                                    return m.group(1)
    except Exception:
        pass
    return ""


def _detect_mc_version(kind: str, path: Path, jars: list[Path]) -> str:
    """检测输入对应 MC 版本（词库下载用）：统计多数 mod 的主流主版本，避免单个异常 jar 误导。

    用户反馈：社区词库应按「分析 mod 版本 → 再匹配下载」顺序，且版本取多数 mod 的主流
    版本而非第一个 jar。限制扫描 200 个 jar（逐读元数据较慢）。
    """
    versions: list[str] = []
    scan_jars = jars if kind == "modjar" else sorted((path / "mods").rglob("*.jar"))
    for jar in scan_jars[:200]:
        v = _jar_mc_version(jar)
        if v:
            versions.append(v)
    if not versions:
        return ""
    # 取主版本（1.20.x → 1.20）众数作为主流版本
    from collections import Counter
    mains = Counter(_MC_VER_RE.match(v).group(1) if _MC_VER_RE.match(v) else v for v in versions)
    main, _ = mains.most_common(1)[0]
    for v in versions:
        m = _MC_VER_RE.match(v)
        if m and m.group(1) == main:
            return v
    return versions[0]


_PACKPACK_README = """【整合包汉化使用说明】
把本文件夹里的全部内容拷进整合包根目录即可（解压即用）：

  mods/I18nUpdateMod.jar       —— i18n 汉化更新 mod，进游戏自动下载 CFPA 社区全量汉化
  mods/vault-patcher.jar       —— Vault Patcher 硬编码汉化 mod（运行时替换 mod 字节码
                                  里写死的硬编码字符串，不修改任何 mod jar）
  vaultpatcher/modules/        —— VP 硬编码汉化映射（自动加载，含翻译的硬编码字符串）
  resourcepacks/模组汉化资源包/  —— 本应用 AI 翻译的补充汉化（补 CFPA 未覆盖的 mod）
  config/ data/ 等             —— 任务书 / 教程 / 进度等文本覆盖
  hardcoded/*-xx化.jar         —— 含 jar 内教程书/进度译文的汉化 jar 副本（若有），
                                需用其替换整合包 mods/ 里对应的原 mod 文件

装好后进游戏：设置 → 资源包 → 启用「模组汉化资源包」；
i18n mod 自动下载并加载全量汉化资源包（联网）；
Vault Patcher 自动加载硬编码汉化映射（无需手动操作）。
"""

_PATCH_README = """【汉化补丁包使用说明】
把整个汉化补丁包解压到整合包根目录，即可覆盖生效（内部路径与整合包根目录精确对齐）：
  config/            → 整合包根目录的 config/
  data/              → 整合包根目录的 data/
  kubejs/            → 整合包根目录的 kubejs/
  scripts/           → 整合包根目录的 scripts/
  vault-patcher.jar  → 请移动到整合包 mods/ 目录（Vault Patcher 模组，已按版本自动下载）
  vaultpatcher/      → 游戏根目录（整合包根目录，VP 加载硬编码映射用）
模组语言文件汉化见「模组汉化资源包.zip」（放入游戏资源包目录）。
若补丁包内没有 vault-patcher.jar（自动下载失败/联网不可用），硬编码汉化会
以 hardcoded/ 目录的汉化 mod jar 形式提供（替换对应 mod）。
覆盖前建议先备份原文件。
"""


def _build_patch_pack(entries: list[tuple[str, str | bytes]], out_path: Path) -> None:
    """生成汉化补丁包 zip：相对路径条目（译文文本或字节，如 VP jar）+ 使用说明.txt。

    条目相对路径由整合包 rglob 相对路径天然生成；双保险白名单校验防穿越
    （不含 .. 与绝对路径）。原整合包只读，翻译内容在调用方渲染完成。
    """
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("使用说明.txt", _PATCH_README)
        for rel, content in entries:
            clean = PurePosixPath(rel)
            if clean.is_absolute() or ".." in clean.parts:
                continue
            zf.writestr(rel, content)


# 运行中任务注册表：保存配置时把新吞吐档位热更新给运行中的 AutoFlow 实例
# （用户诉求：翻译中切换吞吐档位立即生效，无需取消/重启任务）
RUNNING_FLOWS: dict[str, "AutoFlow"] = {}
_flows_lock = threading.Lock()


class AutoFlow:
    """一次自动翻译的编排器（P2-1 流程化拆分）。

    原 970 行巨型函数 run_auto_translation 拆为：__init__ 收集共享上下文（self）、
    run() 编排骨架（识别→分派→扫描→词库→各阶段→产物→收尾）、阶段方法
    （_stage_lang / _stage_json / _stage_hardcode / _stage_build）各自内聚。

    共享状态全存 self（state/memory/engine/产物字典/进度辅助），阶段方法间零参数传递。
    """

    def __init__(self, task_id: str, req: AutoRequest, cfg: AppConfig,
                 store: TaskStore, work_dir: Path, outputs_dir: Path,
                 cfpa_path: Path | None):
        self.task_id = task_id
        self.req = req
        self.cfg = cfg
        # 胡言乱语模式（用户诉求）：搞笑/热梗翻译但忠实原意。测试路径 cfg 可能为 None。
        self.silly = bool((cfg or {}).get("silly_mode"))
        self.store = store
        self.work_dir = work_dir
        self.outputs_dir = outputs_dir
        self.cfpa_path = cfpa_path
        self.state = store.load(task_id)
        self.memory = MemoryStore(work_dir / "memory.json")
        self._resume = False               # 断点续联：True 时 skip/记忆命中不重复计 done（基准已含）
        # v1.3.7 账本式防双计：记录所有已计入 done 的条目唯一键（(归属, key)，
        # 归属=modid/file）。同一 key 重复 bump（记忆命中入队+审查写回、CFPA 命中+
        # 审计等路径双计）直接忽略——从结构上保证 done ≤ total，进度条永不超 100%。
        self._done_keys: set[tuple] = set()
        self._legit_kept: set = set()      # 初审过审的「合理保留原文」(modid,key)——漏翻兜底不再重复审查
        self.project_id = ""               # 项目指纹（run() 早期计算；提前初始化防 finally 收尾 AttributeError）
        # 术语统一（用户诉求：专有名词翻译必须统一，否则乱）：
        # 用户术语表 + 记忆里已确认的短术语对照一起注入 prompt，作为**专名对照（仅提示）**，
        # 让 AI 按语境判断是否遵循（v1.1.0：不再机械替换/强制统一；常用词 light/right 等
        # 绝不统一，多译文冲突由 AI 语境归一化 _ai_contextual_normalize 判定）。
        _gloss = load_glossary(work_dir / "glossary.json")
        self.base_terms: dict[str, str] = dict(_gloss)
        self.glossary_prompt = term_inject_prompt(_gloss)
        _mem_terms = extract_terms(self.memory.data, req.target_lang, max_terms=150)
        self.base_terms.update(_mem_terms)
        if _mem_terms:
            _terms_str = "\n".join(f"{k} => {strip_particle(v)}" for k, v in _mem_terms.items())
            self.glossary_prompt = (self.glossary_prompt + "\n\n"
                                    "已确认术语（翻译时对应当前原文必须严格沿用对应译名，禁止一词多译）：\n"
                                    + _terms_str)
        # CFPA 社区人工词库（中文）：仅中文目标适用——日文/其他语言用它会把中文词条写进
        # 译文（错译），且不应触发下载。**不在 __init__ 同步 load**：索引文件可能几十 MB
        # 几万条，同步解析会阻塞事件循环（→ 任务状态读取超时、流程卡住）；在 run() 里
        # 用 asyncio.to_thread 懒加载已下载索引（复用断点缓存）。
        self.cfpa = {"by_key": {}, "count": 0, "mc_version": "", "size_mb": 0.0}
        # 识别/扫描产物（run 填充，阶段方法消费）
        self.path: Path | None = None
        self.kind: str = ""
        self.source_lang: str = ""
        self.state_jobs: list = []              # 语言文件扫描 jobs（_stage_lang/审计用）
        self.source_by_mod: dict[str, dict[str, str]] = {}   # {modid: {key: 源文本}}（审计/AI 审查共用）
        self.jars: list[Path] = []
        self.pack_sources: list[TextSource] = []
        self.text_sources_by_jar: dict[Path, list[TextSource]] = {}
        self.hard_candidates_by_jar: dict[Path, list[dict]] = {}
        # 硬编码「判断不明确选择不翻译」的候选（严格策略：只翻判断准的；账本校验视为已处置）
        self.hard_unresolved_by_jar: dict[Path, list[str]] = {}
        # 项目级专有名词对照表（用户诉求：Zeno→泽诺 这类决策写词汇表，后续同词统一沿用）：
        # {原文: 决策}，决策为译名（Zeno→泽诺）或原文（保留）。随任务动态积累，注入 prompt
        self.project_terms: dict[str, str] = {}
        # 一致性统计（AI 语境归一化的候选来源）：source -> {译名: 出现次数}——每次写回真译文
        # 累加。build 前 _ai_contextual_normalize 据此收集「同原文 ≥2 译文」的**专名形态**候选，
        # AI 判定语境相同才统一（v1.1.0，替代机械 _consistency_normalize）。
        self._consistency_stats: dict[str, dict[str, int]] = {}
        # 跳过翻译计数（覆盖率分母扣减：可翻译量 = 总文本 - 跳过翻译量）。
        # 跳过 = 技术串 skip（pipeline 判定不需要翻译）+ 硬编码 AI 排除（非用户可见）。
        # 用户诉求：这些本来就不该翻，算进分母会虚低覆盖率。
        self._skipped_n: int = 0
        # 规范译名表 {原文: 规范译名}（v1.1.0）：只对**专名形态**原文登记第一个确认译名，
        # **不强制覆盖**后续译文（多译文冲突由 AI 语境归一化判定）；供 glossary 提示 +
        # AI 归一化审查参考。常用词（light/right）不登记。
        self._norm_terms: dict[str, str] = {}
        self.same_script: bool = False
        self.engine = None          # LLMClient / MachineClient / 兜底引擎
        self.engine_machine: bool = False
        self.engine_filter_technical = None     # 语言文件阶段临时关闭技术串过滤，需恢复
        self._batch_size: int = 20
        # 运行时自适应熔断（v1.2.3 + half-open 恢复）：连续失败降并发保护慢 API，
        # 降档后连续健康自动回升到初始档（不再永久低并发——用户「20 分钟 1000 条 =
        # 并发被压到 2 不恢复」根因）。
        self._circuit_reduced = False
        self._circuit_healthy = 0       # 降档态连续成功批数（满 _CIRCUIT_RECOVER_HEALTHY 回升一档）
        self._circuit_initial_c: int | None = None   # 初始并发（create_engine 后记录，回升封顶）
        self._circuit_initial_b: int | None = None   # 初始批次（同上）
        # 阶段产物（阶段方法写入，run 组织导出）
        self.by_mod: dict[str, dict[str, str]] = {}
        # 请求失败（request_failed）的 (modid, key) 集合（v1.2.8）：审查写回原文后被
        # 漏翻闭环误收集重翻——请求/服务失败重翻无意义且浪费 token、报告失败重复
        self._req_fail_keys: set[tuple[str, str]] = set()
        # v1.2.8 并发生效可视化：当前在飞的并发 chunk 数（聚合「正在翻译 40 条 × N」）
        self._active_chunks: int = 0
        # v1.2.9 审查并发生效可视化：当前在飞的并发审查批数（聚合「静默审查中 N 条 × M」）
        self._active_review: int = 0
        self.json_lines_translations: dict[Path, list[tuple[TextSource, dict[str, str]]]] = {}
        self.pack_translations: list[tuple[TextSource, dict[str, str]]] = []
        self.hard_mappings: dict[Path, dict[str, str]] = {}
        self.hard_excluded_by_jar: dict[Path, list[str]] = {}
        self.failures: list[dict] = []      # 未翻译条目 {text, reason}，翻译结束后置顶展示
        self.exported: list[str] = []       # build 产出的文件路径列表
        self.hard_count: int = 0
        self._aborted: bool = False         # 阶段内取消（原函数级 return）中断整个流程的信号

    # ---------- 引擎 / 进度辅助 ----------

    def _on_usage(self, t_in: int, t_out: int) -> None:
        """token 统计回挂：引擎每批翻译后累加并落盘（前端显示消耗）。"""
        self.state.tokens_in += t_in
        self.state.tokens_out += t_out
        self.store.save(self.state)

    async def _translate_input_name(self) -> None:
        """翻译输入名（整合包/mod/地图/光影文件名，去扩展名）为目标语言。

        右栏标题区：翻译中显示原名，任务完成后显示中文名 + 原英文淡化（用户诉求）。
        单条小请求；AI 失败/保留原文时回退原名（translated 置空），不中断流程。
        """
        if self.path is None:
            return
        # 修复：取名用**原始输入路径**（req.path），不用 self.path.name——run() 里 path 已被
        # 替换成解压缓存目录 extracted/<指纹>/，用它会取到指纹 hash（用户实测项目名
        # 显示 5a818a7428e7 而非整合包名，未完成项目列表/进度文件 name 全错）
        raw = Path(str(self.req.path)).stem.strip()   # zip "X.zip"→"X"；目录无扩展名保持原名
        self.state.display_name = raw
        name_t = raw
        try:
            if self.engine is not None:
                name_t = (await self.engine.translate_batch([raw], self.req.target_lang))[0]
        except Exception:
            name_t = raw
        name_t = (name_t or raw).strip()
        # AI 保留原文/翻译失败 → 置空（前端据此不显示淡化，保持原名）
        self.state.display_name_translated = name_t if (name_t and name_t != raw) else ""
        self.store.save(self.state)

    async def _smart_status(self, preset: str, context: str = "") -> None:
        """阶段状态提示（用户诉求：按阶段有对应提示 + LLM 引擎用 AI 写状态）。

        预设立即 push（流程零等待）；LLM 引擎下后台 fire-and-forget 调 AI 生成一句更
        生动的状态描述，返回后 push 一条 translating（前端合并栏显示最新一条 → 自动
        「升级」为 AI 描述）。机翻/无 AI 引擎/解压扫描阶段（engine 未建）只用预设。
        """
        self.state.progress.append({"status": "translating", "count": 0, "note": preset})
        self.store.save(self.state)
        if not isinstance(self.engine, LLMClient):
            return   # 修复：None 已被 isinstance 短路，冗余检查删除
        # 后台 AI 状态生成：不 await，避免阶段切换被 AI 延迟卡住（慢点无所谓但流程不能被卡）
        asyncio.create_task(self._gen_status_task(preset, context))

    async def _gen_status_task(self, preset: str, context: str) -> None:
        """后台调 AI 生成状态描述并 push；失败/任务已终态静默忽略（预设已 push，不回退）。"""
        try:
            note = await self.engine.generate_status(preset, context)
        except Exception:
            return
        if self.state.status in ("done", "failed", "cancelled"):
            return    # 任务已结束：不再追加状态提示
        if not note or note == preset:
            return    # 修复：AI 失败回退预设时不重复 push 同一条（避免状态条重复叠加）
        self.state.progress.append({"status": "translating", "count": 0, "note": note})
        self.store.save(self.state)

    async def _wait_if_paused(self) -> None:
        """P1-4：暂停等待用 asyncio.Event（即时唤醒）+ 0.5s 超时兜底。

        暂停态 pause_task 清空事件 → wait 阻塞；继续/取消 set → 立即唤醒复查
        paused/cancelled（不再等 0.5s）。超时只是兜底（事件被跨路径直接改 paused 时复查）。
        Y4：取消也 set 事件，取消被暂停卡死的问题一并解决。
        """
        ev = self.store.pause_event(self.task_id)
        while self.state.paused and not self.state.cancelled:
            ev.clear()
            try:
                await asyncio.wait_for(ev.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

    def _set_stage(self, name: str) -> None:
        """切换当前阶段并落盘——前端轮询能立刻读到阶段名变化，消除阶段间无反馈空白。"""
        self.state.stage = name
        self.store.save(self.state)

    def _progress_key(self, modid: str = "", file: str = "", key: str = "") -> tuple:
        """条目进度账本唯一键：(归属, key)。归属 = 文本源文件（json/pack，不同文件
        同 key 是不同条目）或 modid（语言文件，不同 mod 同 key 是不同条目）。
        双计路径（记忆命中入队+审查写回、CFPA 命中等）用同一 key → 账本去重。"""
        return ((file or modid or ""), str(key))

    def _bump_stage(self, n: int = 1, key: tuple | None = None) -> None:
        """推进全局 done 与当前 stage 的 done（进度条阶段明细同步）。

        v1.3.7 账本式防双计（根治「done > total」）：调用方传 key（_progress_key 生成
        的 (归属, key)）时，同一 key 重复 bump 直接忽略——记忆命中入队+审查写回、
        CFPA 命中、skip 等多个路径不会再对同一条目计 2 次（用户实测 4,895/4,588
        超 total 307 根因）。key=None 时无账本直接加（build 产物等单位，无条目 key）。

        stages 里找不到当前 stage（老任务/兼容路径）时只加全局 done，不崩。
        """
        if key is not None:
            if key in self._done_keys:
                return                       # 已计过，防双计
            self._done_keys.add(key)
            n = 1
        self.state.done += n
        for s in self.state.stages:
            if s["name"] == self.state.stage:
                s["done"] += n
                break

    def _bump_stage_only(self, n: int = 1) -> None:
        """续联时已处理（记忆命中/skip）只推进当前 stage 明细，不加全局 done——
        全局基准已含（防翻倍），但 stage 明细要显示实际处理，否则续联看明细从 0 起误以为从头翻。"""
        for s in self.state.stages:
            if s["name"] == self.state.stage:
                s["done"] += n
                break

    async def _ensure_cfpa(self, mc_ver: str) -> dict:
        """词库自动就绪（用户刚需：i18n/CFPA 汉化包内置离线可用）：
        1) 当前内存已就绪且版本匹配 → 直接用；
        2) **应用内置汉化包命中 → 离线加载，零下载零等待**（整合包优先走现成人工
           翻译，不再依赖在线下载——下载失败就全 AI 的历史问题消失）；
        3) 内置缺失 → 在线下载（进度条提示，失败不中断走 AI）。"""
        current = self.cfpa
        want = match_zip_name(mc_ver)
        if current["by_key"] and current.get("mc_version") == want:
            self.state.progress.append({"status": "done", "key": "社区词库", "source": mc_ver,
                                        "translated": f"已就绪 {current['count']} 词条"})
            self.store.save(self.state)
            return current
        # 优先内置汉化包（离线可用，整合包刚需）
        try:
            bundled = await asyncio.to_thread(load_bundled_cfpa, mc_ver)
        except Exception:
            bundled = None
        if bundled and bundled.get("by_key"):
            self.cfpa = bundled
            self.state.progress.append({"status": "done", "key": "社区词库", "source": mc_ver,
                                        "translated": f"内置汉化包 {bundled['count']} 词条"
                                                      f"（{bundled.get('size_mb', 0)}MB）"})
            self.store.save(self.state)
            return bundled
        # 直接 push（不用 _smart_status）：下载阶段进度优先，AI 状态生成会再推一条
        # 造成「正在下载词库」两条叠加（用户反馈）
        self.state.progress.append({"status": "translating", "count": 0,
                                    "note": f"正在下载社区词库（MC {mc_ver}）…"})
        self.store.save(self.state)
        try:
            g = await download_cfpa(mc_ver, self.cfpa_path)
        except Exception as exc:
            self.state.progress.append({"status": "warn",
                                        "error": f"社区词库下载失败（{type(exc).__name__}），走 AI 翻译"})
            self.store.save(self.state)
            return {"by_key": {}, "count": 0}
        if not g:
            self.state.progress.append({"status": "warn",
                                        "error": "社区词库下载失败（网络不可用或版本无匹配），走 AI 翻译"})
            self.store.save(self.state)
            return {"by_key": {}, "count": 0}
        self.state.progress.append({"status": "done", "key": "社区词库", "source": mc_ver,
                                    "translated": f"已下载 {g['count']} 词条"})
        self.store.save(self.state)
        return g

    # v1.1.0：_term_map / _protect_terms / _restore_terms（机械术语占位符保护）已删除——
    # 它是「right 翻成右面后所有 right 全替换成右面」破坏语境的机械元凶；
    # 归一化改为 AI 语境判定（glossary 仅提示 + _ai_contextual_normalize 审查）。

    async def _engine_translate(self, texts: list[str], reasons=None, **kw) -> tuple[list[str], dict]:
        """引擎批量翻译包装：返回 (results, meta)。

        v1.1.0：**移除机械术语保护**（_protect_terms 占位符替换——它是「right 被翻译成右面
        后所有 right 全替换成右面」破坏语境的元凶）。归一化只通过 glossary_prompt 提示 AI
        （专名对照），AI 按语境自行判断遵循；同原文多译文的语境判定由 AI 语境归一化审查
        （_ai_contextual_normalize）在 build 前处理，不再翻译时硬替换。

        meta 携带本次调用的失败标记/错误类别/致命错误（per-call 隔离）——修复并行管道
        共享同一 engine 实例时实例属性 clear() 互相污染（请求失败被误判「AI 故意保留」
        → 覆盖率 0 的假成功）：LLMClient 走 translate_batch(meta=...) 拿本次调用 ctx；
        机翻/兜底引擎无 meta 协议，翻译后从实例属性回读（其 translate_batch 本身串行）。
        """
        meta = {}
        reasons = list(reasons) if reasons else None
        _is_llm = isinstance(self.engine, LLMClient)
        masked = list(texts)   # v1.1.0：直接传原文（无占位符保护，AI 按语境自由翻译）
        if _is_llm:
            try:
                results = await self.engine.translate_batch(
                    masked, self.req.target_lang, meta=meta, feedback=reasons or None, **kw)
            except ValueError:
                # v1.4.6 修复：fatal（401/403）已在 meta 里 update（translate_batch 先
                # meta.update(ctx) 再抛 ValueError）。捕获后返回整批原文 + fatal meta，
                # 让 _flush 的 `_fatal = _meta.get("fatal")` 读到——否则异常向上传播、
                # _flush 的 except 置 _meta=None，401 被吞、任务每批白打一次 401。
                return list(texts), meta
        else:
            # 非 LLM 引擎（machine/兜底）不接 LLM 专用参数（forced/feedback——修复 Agent 审查：
            # 漏翻专项重翻传 forced=True → MachineClient.translate_batch 签名不含该参抛
            # TypeError，机翻漏翻重翻静默报废、漏翻无法修复）
            _llm_kw = {k: v for k, v in kw.items() if k not in ("forced", "feedback")}
            results = await self.engine.translate_batch(masked, self.req.target_lang, **_llm_kw)
            meta = {"failed": set(getattr(self.engine, "_batch_failed_texts", ())),
                    "kind": getattr(self.engine, "_last_error_kind", "other"),
                    "fatal": getattr(self.engine, "_fatal_error", None)}
        # v1.1.0：无占位符保护，failed 已是原始 source，无需还原
        return results, meta

    async def _wait_network_retry(self, translate_fn, texts: list[str],
                                  reasons: list[str], max_total_time: float = _RETRY_MAX_TOTAL_TIME,
                                  initial_kind: str = "other") -> list | None:
        """网络/限流失败：指数退避+抖动重试，90s总超时放弃。

        v1.4.5（业界最佳实践）：
        - 区分错误类型：限流等更久（10-60秒），超时/网络等更短（2-30秒）
        - 指数退避+随机抖动：防止惊群效应
        - 总超时90秒，不是重试次数
        - 尊重 Retry-After 头（如果API返回）

        v1.4.6：接受 initial_kind（实际错误类型）做首次退避基准；translate_fn 用
        wait_for 包住、超时=剩余时间，避免单次阻塞 180s 超过总超时（实际等待远超声明）。
        """
        start_time = time.monotonic()
        attempt = 0
        _last_kind = initial_kind or "other"

        while not self.state.cancelled:
            elapsed = time.monotonic() - start_time
            if elapsed >= max_total_time:
                return None  # 总超时放弃

            await self._wait_if_paused()

            # 计算本次等待时间（指数退避+抖动）
            params = _RETRY_PARAMS.get(_last_kind, _RETRY_PARAMS["other"])
            base_delay = min(params["base"] * (2 ** attempt), params["max"])
            jitter = random.uniform(0, base_delay * 0.5)  # 50%抖动
            wait_time = base_delay + jitter

            # 确保不超过总超时
            remaining = max_total_time - elapsed
            if wait_time > remaining:
                wait_time = max(1.0, remaining)

            self.state.progress.append({"status": "translating", "count": len(texts),
                                        "note": f"等待网络恢复（{wait_time:.0f}s 后重试，已用{elapsed:.0f}s/90s）…"})
            self.store.save(self.state)

            # 等待（每秒检查取消状态）
            for _ in range(int(wait_time)):
                if self.state.cancelled:
                    break
                await asyncio.sleep(1)
            if self.state.cancelled:
                return None

            try:
                # v1.4.6：wait_for 包住 translate_fn，超时=剩余时间——原直接 await，
                # 单次请求最多阻塞 180s（httpx timeout）超过 90s 总超时声明
                _timeout = max(5.0, remaining)
                _got = await asyncio.wait_for(translate_fn(texts, reasons), timeout=_timeout)
                if isinstance(_got, tuple) and len(_got) == 2 and isinstance(_got[1], dict):
                    got, meta = _got
                else:
                    got, meta = _got, None

                _failed = (meta["failed"] if meta
                           else (getattr(self.engine, "_batch_failed_texts", ()) if self.engine else ()))
                _kind = (meta["kind"] if meta
                         else (getattr(self.engine, "_last_error_kind", "other") if self.engine else "other"))
                _last_kind = _kind

                if _kind == "auth":
                    _f = (meta.get("fatal") if meta
                          else (getattr(self.engine, "_fatal_error", None) or "API Key 无效或无权限"))
                    raise ValueError(f"翻译失败：{_f}")

                if not any(t in _failed for t in texts):
                    return got  # 网络恢复，重试成功
            except ValueError:
                raise
            except Exception:
                pass

            attempt += 1

        return None

    async def _translate_batch_pipeline(self, items, translate_fn, batch_size: int = 20,
                                         skip_fn=needs_translation,
                                         force_engine: bool = False,
                                         keep_original_ok: bool = True,
                                         count_done: bool = True,
                                         enqueue_fn=None) -> None:
        """批量翻译流水线（语言文件 / json-lines / 兜底硬编码共用）。

        逐条预处理（已汉化跳过 / 记忆命中 / 简繁直转）在批外完成，只有
        真正需要走引擎的条目才收集成批；攒满 batch_size 一次 translate_batch，
        结果逐条写回记忆/产物/进度。批之间响应取消与暂停。

        items: 可迭代对象，元素为 {"key", "text", "sink"}（sink 为写回产物字典）；
        translate_fn: async (texts: list[str]) -> list[str]，批量走引擎。
        skip_fn: 单条「是否跳过翻译」判定，默认 needs_translation（技术串过滤）；
        语言文件阶段传 needs_lang_value_translation（仅已汉化判断，放行 snake_case 值）。
        keep_original_ok: 「引擎返回原文」不计 failed 的开关，默认 True（对齐主流汉化工具
        languageModelAttemptCount 的接受判定：译文存在且格式完整即成功，不把「AI 故意
        保留原文」当失败——专有名词 Minecraft、命令 /give、代码标识 ModelViewMat 等
        AI 保留原文是正常决策）。区分真失败靠 _batch_err：仅「请求异常/降级均失败」
        回原文才计 failed。
        """
        pending: list[dict] = []          # 待引擎条目 {key, text, sink, reason?}

        def _push_chunk_status(n: int) -> None:
            """聚合一条「正在翻译 N 条 × 当前并发」：明细只显示最新一条（前端去重）。"""
            self.state.progress.append({"status": "translating", "count": n,
                                        "active": self._active_chunks, "key": "@active_chunks"})
            self.store.save(self.state)

        def _chunk_start_cb(n: int) -> None:
            """每并发 chunk 请求开始：在飞计数 +1 → 聚合提示递增。

            **嵌套函数**（非实例方法）：作为 engine.on_chunk_start 回调被调（只收 n），
            self 走闭包——写成 `def _chunk_start_cb(self, n)` 会 AttributeError 整批失败。
            """
            self._active_chunks += 1
            _push_chunk_status(n)

        def _chunk_done_cb(n: int) -> None:
            """每并发 chunk 完成（成败都）：在飞计数 -1 → 聚合提示递减。"""
            self._active_chunks = max(0, self._active_chunks - 1)
            _push_chunk_status(n)

        async def _flush(batch: list[dict]) -> None:
            """翻译一批（batch 参数）→ 逐条写回记忆/产物/进度。

            v1.4.6：改为接受 batch 参数（worker pool 并发调用，不再串行 await）。
            内部用 pending 别名保持原逻辑引用不变；batch 是独立列表（主循环每批新建），
            不 clear 不影响主循环的 pending 变量。
            """
            pending = batch
            if not pending:
                return
            texts = [p["text"] for p in pending]
            # v1.2.8：进度反馈改为「聚合一条『正在翻译 N 条 × 当前并发』」——LLM 引擎
            # 每 chunk 开始/完成更新在飞计数，明细只显示最新一条（并发 16 → 「正在翻译
            # 40 条 × 16」→ 随完成递减，不是 16 条刷屏）；无该能力的引擎（machine）退回
            # 整批「正在翻译 N 条」提示（原行为，批量请求期间进度条也有反馈）
            _eng = getattr(self, "engine", None)
            _chunk_ok = bool(_eng is not None and hasattr(_eng, "on_chunk_start"))
            if not _chunk_ok:
                self.state.progress.append({"status": "translating", "count": len(pending)})
            self.store.save(self.state)
            try:
                # translate_fn(texts, reasons)：reasons 与 texts 对齐，供 AI 审查反馈重翻
                # （每条携带上次审查不合格原因，AI 针对原因修正——用户诉求「翻译到合格」）
                # v1.4.6：回调已由 worker pool 在外层一次性挂载（并发 flush 共用），
                # 这里不再反复设置/恢复（并发下会互相覆盖）；_active_chunks 由
                # _chunk_start_cb/_chunk_done_cb 的 +1/-1 维护，不在此清零
                if _chunk_ok:
                    _translated = await translate_fn(
                        texts, [p.get("reason", "") for p in pending])
                else:
                    _translated = await translate_fn(
                        texts, [p.get("reason", "") for p in pending])
                # 修复：translate_fn 返回 (results, meta)（per-call 失败状态）或 list（mock/旧引擎）
                if (isinstance(_translated, tuple) and len(_translated) == 2
                        and isinstance(_translated[1], dict)):
                    translated_list, _meta = _translated
                else:
                    translated_list, _meta = _translated, None
                _batch_err = ""            # 批请求成功，逐条「返回原文」才记失败
            except Exception as exc:
                translated_list = None
                _meta = None
                # 修复：异常消息并入 _batch_err——"未配置" 判断依赖消息文本，只记类型名是死分支
                _batch_err = f"请求异常：{type(exc).__name__}: {exc}"
            # 鉴权致命错误（API key 无效/未配置）：重试无用，立即失败并提示（配置问题）
            _fatal = _meta.get("fatal") if _meta else getattr(self.engine, "_fatal_error", None)
            if _fatal or (translated_list is None and "未配置" in (_batch_err or "")):
                raise ValueError(f"翻译失败：{_fatal or _batch_err}")
            # 错误分类（修复「无论怎样都显示网络超时」）：只有可恢复错误（timeout/network/
            # ratelimit/server 5xx）才攒批重试；rejected（4xx 配置/数据错）/other 是
            # 不可恢复，明确记 failed 带原因，绝不假装网络超时无限等待。
            _failed = (_meta.get("failed") if _meta
                       else (set(getattr(self.engine, "_batch_failed_texts", ())) if self.engine else set()))
            _kind = (_meta.get("kind") if _meta
                     else (getattr(self.engine, "_last_error_kind", "other") if self.engine else "other"))
            _retryable = _kind in ("timeout", "network", "ratelimit", "server")
            _failed_set = set(_failed or ())
            _has_fail = bool(_failed_set and any(t in _failed_set for t in texts))
            if _retryable and (_has_fail or translated_list is None):
                # v1.2.3：失败**子集**攒批重试（封顶）——不再整批断网无限等待、不假装网络超时。
                # 失败子集：整批异常取全 pending；部分失败取 _failed 命中的条目。
                if translated_list is None:
                    retry_pos = list(range(len(pending)))
                else:
                    retry_pos = [k for k, p in enumerate(pending) if p["text"] in _failed_set]
                # 连通性检查一次：网络通但请求失败 → 服务/配置问题，攒批重试；
                # 无连通性检查能力（mock/旧引擎）→ 走断网等待重试（兼容测试/降级）
                _online = False
                if hasattr(self.engine, "check_connectivity"):
                    try:
                        _online = await self.engine.check_connectivity()
                    except Exception:
                        _online = False
                if not _online:
                    # 真断网 → 退避重试（_wait_network_retry 内部封顶 max_attempts，不再无限等待）
                    _note = {"timeout": "网络超时", "network": "网络连接失败",
                             "ratelimit": "请求被限流", "server": "翻译服务暂时不可用"}.get(_kind, "网络中断")
                    self.state.progress.append({"status": "translating", "count": len(pending),
                                                "note": f"{_note}，等待网络恢复…"})
                    self.store.save(self.state)
                    _retried = await self._wait_network_retry(
                        translate_fn, [pending[k]["text"] for k in retry_pos],
                        [pending[k].get("reason", "") for k in retry_pos],
                        initial_kind=_kind)
                    if _retried is None:
                        if self.state.cancelled:
                            return   # 用户取消，中断整个流程
                        # 封顶仍失败 → 明确记 failed（不无限假装网络等待）
                        _batch_err = _batch_err or "网络持续不可用，多次重试仍失败"
                        _failed = {pending[k]["text"] for k in retry_pos}
                        _has_fail = True
                        if translated_list is None:
                            translated_list = [p["text"] for p in pending]
                        for k in retry_pos:
                            translated_list[k] = pending[k]["text"]
                    else:
                        # 网络恢复，失败子集整批重试成功
                        if translated_list is None:
                            translated_list = [p["text"] for p in pending]
                        for k, tr in zip(retry_pos, _retried):
                            translated_list[k] = tr
                        _batch_err = ""
                        _failed = set()
                        _has_fail = False
                else:
                    # 网络连通：失败子集攒批重试（90s总超时，指数退避+抖动）
                    # v1.4.5：业界最佳实践——指数退避+抖动，区分错误类型
                    _start_time = time.monotonic()
                    _attempt = 0
                    _last_kind = _kind

                    while retry_pos:
                        elapsed = time.monotonic() - _start_time
                        if elapsed >= _RETRY_MAX_TOTAL_TIME:
                            break  # 总超时放弃

                        # 指数退避+抖动
                        if _attempt > 0:
                            params = _RETRY_PARAMS.get(_last_kind, _RETRY_PARAMS["other"])
                            base_delay = min(params["base"] * (2 ** (_attempt - 1)), params["max"])
                            jitter = random.uniform(0, base_delay * 0.5)
                            wait_time = base_delay + jitter
                            remaining = _RETRY_MAX_TOTAL_TIME - elapsed
                            wait_time = min(wait_time, remaining)
                            await asyncio.sleep(wait_time)

                        _rp = [pending[k] for k in retry_pos]
                        _r_texts = [p["text"] for p in _rp]
                        _r_reasons = [p.get("reason", "") for p in _rp]
                        try:
                            _retried, _r_meta = await translate_fn(_r_texts, _r_reasons)
                        except Exception:
                            _retried, _r_meta = None, None
                        if _retried is None:
                            break
                        if translated_list is None:
                            translated_list = [p["text"] for p in pending]
                        _r_failed = set((_r_meta or {}).get("failed") or ()) if _r_meta else set()
                        _r_kind = (_r_meta or {}).get("kind", "other")
                        _last_kind = _r_kind
                        for k, tr in zip(retry_pos, _retried):
                            translated_list[k] = tr
                        retry_pos = [k for k in retry_pos if pending[k]["text"] in _r_failed]
                        _attempt += 1

                    # 超时后仍失败 → 明确记 failed
                    if retry_pos:
                        _batch_err = _batch_err or "多次重试仍失败（服务不稳定）"
                        _failed = {pending[k]["text"] for k in retry_pos}
                        _has_fail = True
                    else:
                        _batch_err = ""
                        _failed = set()
                        _has_fail = False
            elif translated_list is None or _has_fail:
                # 不可恢复失败（4xx 配置/数据错误等）：不假装网络超时，明确记 failed 带原因
                if translated_list is None:
                    translated_list = [p["text"] for p in pending]
                _batch_err = _batch_err or f"请求被拒绝（{_kind}），请检查 API 配置"
            # 运行时自适应熔断（v1.2.3 + half-open 恢复）：连续可恢复失败（限流/超时/网络/
            # server）≥3 次 → 自动降并发/批次（保护慢 API）；**降档后连续健康（满 10 批）
            # 自动回升一档直到初始档**——月食降档不再永久压制并发（用户「MiniMax 20 分钟
            # 1000 条」根因：降下去永不恢复，一直 1-2 并发慢跑）。
            _CIRCUIT_RECOVER_HEALTHY = 10   # 降档态连续成功批数 → 回升一档（half-open probe）
            if isinstance(self.engine, LLMClient):
                _cc = getattr(self.engine, "_consec_fails", 0)
                if _has_fail and _retryable:
                    self.engine._consec_fails = _cc + 1
                    self._circuit_healthy = 0          # 失败打断健康计数
                else:
                    self.engine._consec_fails = 0
                    # 降档态且本批健康 → 累计，满阈值回升一档（封顶初始档）
                    if self._circuit_reduced:
                        self._circuit_healthy += 1
                        if self._circuit_healthy >= _CIRCUIT_RECOVER_HEALTHY:
                            _cur_c = getattr(self.engine, "concurrency", 5) or 5
                            _cur_b = getattr(self.engine, "batch_size", 20) or 20
                            _ic = self._circuit_initial_c or _cur_c
                            _ib = self._circuit_initial_b or _cur_b
                            _new_c = min(_ic, _cur_c * 2)
                            _new_b = min(_ib, _cur_b * 2)
                            if _new_c != _cur_c or _new_b != _cur_b:
                                self.set_throughput(concurrency=_new_c, batch_size=_new_b)
                                self.state.progress.append({"status": "translating", "count": len(pending),
                                                            "note": f"API 已恢复稳定，自动回升并发（{_cur_c}→{_new_c} · 批 {_cur_b}→{_new_b}）"})
                                self.store.save(self.state)
                            self._circuit_healthy = 0
                            if (_new_c, _new_b) == (_ic, _ib):
                                self._circuit_reduced = False   # 回到初始档，退出降档态
                # 触发降档（v1.2.9 弱化为兜底）：RateGate 请求前限速已保证 API 永不 429，
                # 「撞限流→降档」主触发源消失（动态测试校准后 RateGate 更稳）——仅当网络/
                # 服务**持续故障**（连续失败 ≥8 次）才降并发保护，作为极端兜底而非常驻机制。
                if self.engine._consec_fails >= 8:
                    _cur_c = getattr(self.engine, "concurrency", 5) or 5
                    _cur_b = getattr(self.engine, "batch_size", 20) or 20
                    _new_c = max(1, _cur_c // 2)
                    _new_b = max(4, _cur_b // 2)
                    if _new_c != _cur_c or _new_b != _cur_b:
                        self.set_throughput(concurrency=_new_c, batch_size=_new_b)
                        self.state.progress.append({"status": "translating", "count": len(pending),
                                                    "note": f"当前 API 限流/超时频繁，已自动降低并发（{_cur_c}→{_new_c} · 批 {_cur_b}→{_new_b}）"})
                        self.store.save(self.state)
                    self.engine._consec_fails = 0
                    self._circuit_reduced = True
                    self._circuit_healthy = 0
            # 防 zip 截断：引擎返回条数不足 pending 时，尾部条目静默丢失会让
            # done<total、进度卡 <100%、产物缺条目（审查 P1-2）→ 按原文补足并计 failed
            if len(translated_list) != len(pending):
                for _ in range(len(pending) - len(translated_list)):
                    translated_list.append(pending[len(translated_list)]["text"])
                _batch_err = _batch_err or "引擎返回条数不足"
            for p, translated in zip(pending, translated_list):
                key, text, sink = p["key"], p["text"], p["sink"]
                # 修复换行：AI 返回字面 \n（反斜杠 n 两字符），写入 json 时不转义 → 游戏
                # 显示字面 \n。转成真实换行符，json.dumps 自动转义成 JSON 标准 \n。
                translated = translated.replace("\\n", "\n")
                # 修复：清除无效 surrogate（LLM 输出可能产生，ensure_ascii=False 写盘 utf-8
                # 抛 "surrogates not allowed" 崩溃——用户实测翻译 2 小时炸）
                translated = clean_surrogates(translated)
                # 入队模式（enqueue_fn 并行审查）：翻译结果交审查队列，**不直接写回**——
                # 审查管道审查通过才写 by_mod/明细/记忆（审查是翻译的后续，边翻译边审查）
                if enqueue_fn is not None:
                    # 修复：enqueue 分支也判定「请求失败回原文」——审查请求失败（issues=[]）时
                    # 真失败会被当「AI 保留」放行不 failed（Agent 审查确认），带标记让审查计失败
                    _real_fail = (text in _failed) if _failed is not None else False
                    # 修复（recheck #1）：请求失败判定必须**逐条**——_batch_err 是整批级标记，
                    # 批内部分失败时成功译文会被误标 request_failed → 审查写回原文 + failed（好译文丢失）。
                    # v1.2.9 再修（Agent recheck）：`translated == text and bool(_batch_err)` 会误伤
                    # 「AI 故意保留原文」的专有名词条目（同批其他条目失败时被误判 request_failed →
                    # 记 failed）。_failed 集合已精确记录失败原文，**只基于 _real_fail** 判定——
                    # AI 保留原文由审查判「合理保留 or 漏翻重翻」，不因批内他条失败背锅。
                    _req_fail = _real_fail
                    # sink：审查管道写回目标（lang=by_mod[modid]，json/pack=该文本源 out dict）
                    # v1.3.7：带 file 供 _write_reviewed 账本 key（_progress_key）一致——
                    # json/pack 阶段同 key 不同文件是不同条目，账本靠 (file, key) 区分
                    await enqueue_fn({"key": key, "modid": p.get("mod", ""),
                                      "file": p.get("file", ""),
                                      "source": text, "translated": translated,
                                      "sink": sink, "request_failed": _req_fail})
                    # v1.2.9 用户诉求：翻译 done **不在翻译时加**——等审查过关写回才加
                    # （_write_reviewed 里 bump）——进度条读数 = 已过审成品，翻译与审查对齐，
                    # 审查完一批读数同步、直接进下一轮。request_failed 条目由审查计 failed。
                    continue
                if translated == text:
                    # 区分「AI 故意保留原文」vs「请求失败回原文」：llm 在请求失败/超时把原文
                    # 记入 _batch_failed_texts。真失败必须记 failed，否则 keep_original_ok
                    # 会把原文当「AI 保留」放行 → 覆盖率为 0 的假成功。
                    _real_fail = (text in _failed) if _failed is not None else False
                    # v1.3.0 修复（用户「不含汉字的大段英文判过」）：keep_original_ok 只放行
                    # **无英文残留**的原文（专名 Xaero/Balm、短词、%s 占位符、命令——用
                    # _has_english_leak 判，其内部已豁免合理保留/路径/命令）——否则 json/pack
                    # 阶段（无审查管道）AI 偷懒返回**大段英文**被当「AI 故意保留」写回英文、
                    # 不 failed（整批英文判过根因）。大段英文（≥3 词）走 failed（不写 sink →
                    # 产物缺该 key，游戏回退原文）。单专名不受影响。
                    if keep_original_ok and not _batch_err and not _real_fail \
                            and not self._has_english_leak(text):
                        # AI 故意保留原文（专有名词/命令/代码标识）→ 不算失败，写回原文
                        # 供审查判漏翻（该翻的会被审查抓出重翻；专有名词审查豁免）
                        sink[key] = translated
                        if count_done:
                            self._bump_stage(key=self._progress_key(p.get("mod", ""), p.get("file", ""), key))
                    else:
                        # 失败回原文：仅主轮（count_done）计 failed——重翻轮不重复计，
                        # 终审 _record_residual_failures 统一判（修复：同一失败条目计 4-6 次）
                        # **不写 sink**：保留已有合格译文（「宁可有瑕疵译文」策略，失败
                        # 不覆盖好译文——审查重翻失败时旧译文还在，不被击穿成漏翻）
                        if count_done:
                            self.state.failed += 1
                            # 记录 key（修复 Agent 审查）：_record_residual_failures 据此跳过
                            # 已计 failed 的 key，消除「流水线真失败 + 审计缺译文」双计
                            # 修复（recheck 双计）：failures 进 count_done 块——重翻轮失败
                            # 不再重复 append，报告条目数与顶部 failed 计数对得上
                            self.failures.append({"key": key, "text": text[:50],
                                                  "reason": _batch_err or ("翻译服务失败" if _real_fail else "LLM 未返回译文")})
                else:
                    # 真译文：名称归一化（第一定义/后续跟随）→ 写 sink + 写记忆。
                    # 无审查管道的引擎（machine/兜底）在此也做归一化，保证全引擎统一。
                    translated = self._apply_name_norm(text, translated)
                    self.memory.set(text, self.req.target_lang, translated)
                    self._record_consistency(text, translated)   # 一致性统计（兜底归一化用）
                    sink[key] = translated
                    if count_done:
                        self._bump_stage(key=self._progress_key(p.get("mod", ""), p.get("file", ""), key))
                # 修复（recheck 双计）：done 只在成功/保留原文分支 bump——失败条目仅计 failed，
                # 否则 done+failed>total、覆盖率分子含失败条目虚高
                self.state.progress.append({"key": key, "source": text,
                                            "translated": translated, "status": "done"})
                if self.state.done % 10 == 0:
                    self.memory.save()
                    self.store.save(self.state)
            # 批末无条件落盘（关键）：done/progress 若停在不满足 %10 的值，
            # 前端轮询 getTask 读盘旧值 → 进度条/明细「卡住不涨」（用户反馈）
            if enqueue_fn is None:
                self.memory.save()
            self.store.save(self.state)
            # 断点续联实时保存（用户诉求彻底修复）：_save_progress 原本只在任务终态
            # finally 调一次，而桌面版关窗是 os._exit(0) 直接杀进程、finally 不执行 →
            # progress 从不落盘 → 重启后「未完成项目」列表为空（无法续联 / 名称回退哈希）。
            # 这里每批末节流（≥2s）写一次 progress：项目指纹 + 真实名 + 原始路径 + 实时进度。
            _now = time.time()
            if _now - getattr(self, "_last_progress_save", 0) > 2:
                try:
                    self._save_progress()
                except Exception:
                    pass
                self._last_progress_save = _now
            # v1.4.6：不再 pending.clear()——batch 是独立列表（主循环每批新建传参），
            # clear 只影响本次 batch，无意义且干扰并发（worker pool 下 batch 独立）。

        # v1.4.6 worker pool 补位制度（用户诉求：攒够 batch_size 就提交翻译，并行 = 同一
        # 时间能跑的数量，前面完成后面补位，不是一批等一批）：
        # - 攒够 batch_size → create_task 提交 _flush（不 await，主循环继续攒下一批）
        # - _flush_sem 限制同时运行的 flush 数 = 引擎并发（translate_batch 每批 1 个请求）
        # - 前面的完成释放槽位 → 排队的自动补位（信号量天然实现）
        _flush_sem = asyncio.Semaphore(
            max(1, int(getattr(self.engine, "concurrency", 1) or 1)))
        _flush_tasks: list[asyncio.Task] = []
        # v1.4.6：on_chunk 回调一次性挂载（并发 flush 共用），收尾恢复原值——
        # 原来每次 _flush 设置/恢复，并发时互相覆盖导致进度显示失效
        _cbe = getattr(self, "engine", None)
        _saved_ocs = _saved_ocd = None
        _cb_ok = bool(_cbe is not None and hasattr(_cbe, "on_chunk_start"))
        if _cb_ok:
            _saved_ocs, _saved_ocd = _cbe.on_chunk_start, _cbe.on_chunk_done
            _cbe.on_chunk_start = _chunk_start_cb
            _cbe.on_chunk_done = _chunk_done_cb

        async def _flush_limited(batch: list[dict]) -> None:
            """拿槽位后翻译一批（信号量控制并发，完成即补位）。"""
            async with _flush_sem:
                await _flush(batch)

        for item in items:
            if self.state.cancelled:
                self.state.status = "cancelled"
                self.store.save(self.state)
                self._aborted = True   # 修复：软取消也中断整个流程（否则继续 build 覆盖 status=done）
                return
            await self._wait_if_paused()
            key, text, sink = item["key"], item["text"], item["sink"]
            # mod/file 归属（整合包右侧明细需要知道「在翻译哪个 mod / 哪个配置文件」，
            # 否则几千条 key 全无归属，用户看到的就是笼统的「翻译 config / 翻译 mod」）
            src_mod = item.get("mod", "")
            src_file = item.get("file", "")
            # v1.3.2 修复（用户「Minimaxi→迷你极巨」）：painting.*.author / ftbquests 任务
            # author 字段是**作者名（MC 用户名/专名）**，不该翻译——AI 把 "Minimaxi" 意译成
            # 「迷你极巨」。跳过翻译（不入产物 → 游戏保留原作者名）；画作标题 .title 照常翻。
            if not force_engine and not self.same_script and (
                    not skip_fn(text, self.req.target_lang) or str(key).endswith(".author")):
                # 已汉化（含 CJK）/ 技术串 / 作者名：跳过翻译，计 done，不入产物。
                # 注意：same_script（简繁互转）时中文源文本必须保留翻译，跳过会漏转繁体。
                self._skipped_n += 1   # 跳过翻译计数（覆盖率分母扣：可翻译量 = 总文本 - 跳过）
                if count_done:
                    # 续联：skip 只推进 stage 明细（全局基准已含，防翻倍）
                    self._bump_stage(key=self._progress_key(src_mod, src_file, key)) \
                        if not self._resume else self._bump_stage_only()
                self.state.progress.append({"key": key, "source": text,
                                            "translated": text, "status": "done",
                                            "mod": src_mod, "file": src_file})
                self.store.save(self.state)   # P1-5：skip 路径也落盘，避免累计不足 10 条时前端进度「卡住不涨」
                continue
            cached = self.memory.get(text, self.req.target_lang) if not force_engine else None
            if cached:
                # 名称归一化：本次任务已登记规范译名 → 权威优先于记忆旧译名
                # （防历史次优译名残留——老版本未归一化时写的译名绕开审查）
                norm = self._norm_terms.get(text) if not force_engine else None
                if norm:
                    cached = norm
                # 记忆命中：直接写回。修复：**续联时命中不再入审查管道**（词条已审查过，
                # 重复审查浪费 token 且拖慢续联——用户诉求「连审查都重跑」）；非续联
                # 正常模式才交审查管道（审查通过才写明细）
                if enqueue_fn is not None and not self._resume:
                    # v1.3.7 根治双计（用户「4,895/4,588 超 total」）：入队审查管道
                    # 时**不提前 bump**——done 由审查写回 _write_reviewed 统一加
                    #（对齐简繁分支 v1.2.9 修复，记忆命中分支此前漏修→同 key 计 2 次）。
                    # enqueue dict 带 file 供 _write_reviewed 账本 key（_progress_key）一致。
                    await enqueue_fn({"key": key, "modid": src_mod, "file": src_file,
                                      "source": text, "translated": cached, "sink": sink})
                else:
                    sink[key] = cached
                    self._record_consistency(text, cached)   # 一致性统计（归一化用）
                    self.state.progress.append({"key": key, "source": text,
                                                "translated": cached, "status": "done",
                                                "mod": src_mod, "file": src_file})
                    if count_done:
                        # 续联：记忆命中只推进 stage 明细（全局基准已含）
                        self._bump_stage(key=self._progress_key(src_mod, src_file, key)) \
                            if not self._resume else self._bump_stage_only()
                if self.state.done % 10 == 0:
                    self.memory.save()
                self.store.save(self.state)
                continue
            if self.same_script:
                # 简繁双向直转，免 AI：zh_tw 走繁化，zh_cn 走简化（F5）
                translated = (traditional(text) if self.req.target_lang == "zh_tw" else simplify(text))
                if enqueue_fn is not None:
                    # v1.3.7：enqueue dict 带 file 供 _write_reviewed 账本 key 一致
                    await enqueue_fn({"key": key, "modid": src_mod, "file": src_file,
                                      "source": text, "translated": translated, "sink": sink})
                    # v1.2.9 修复（recheck 双计）：简繁直转入审查队列也不提前 bump——
                    # 审查过关写回 _write_reviewed 统一 bump，否则同一条目 done 加 2 次
                else:
                    self.memory.set(text, self.req.target_lang, translated)
                    sink[key] = translated
                    if count_done:
                        # 续联：简繁直转只推进 stage 明细（全局基准已含）
                        self._bump_stage(key=self._progress_key(src_mod, src_file, key)) \
                            if not self._resume else self._bump_stage_only()
                if enqueue_fn is None:
                    self.state.progress.append({"key": key, "source": text,
                                                "translated": translated, "status": "done",
                                                "mod": src_mod, "file": src_file})
                    if self.state.done % 10 == 0:
                        self.memory.save()
                self.store.save(self.state)
                continue
            # 需走引擎：收集入批，攒满 batch_size 提交翻译任务（worker pool，不 await）
            # reason（审查不合格原因）：review 重翻时携带，feedback 注入 prompt 让 AI 针对修正
            pending.append({"key": key, "text": text, "sink": sink,
                            "reason": item.get("reason", ""),
                            "mod": src_mod, "file": src_file})
            # v1.4.6 worker pool 补位：攒够 batch_size 就 create_task 提交（不 await），
            # 主循环继续攒下一批；_flush_sem 限制并发数，前面完成槽位释放后面自动补位。
            # batch_size=1（漏翻逐条重翻）保持逐条专注不放大。
            if len(pending) >= batch_size:
                _flush_tasks.append(asyncio.create_task(_flush_limited(pending)))
                pending = []
        # 收尾：剩余不足 batch_size 的批次也提交，等所有翻译任务完成
        if pending:
            _flush_tasks.append(asyncio.create_task(_flush_limited(pending)))
        if _flush_tasks:
            await asyncio.gather(*_flush_tasks)
        # 恢复 on_chunk 回调（worker pool 结束，防影响后续阶段/任务）
        if _cb_ok:
            try:
                _cbe.on_chunk_start, _cbe.on_chunk_done = _saved_ocs, _saved_ocd
            except Exception:
                pass
        self._active_chunks = 0

    # ---------- 审查管道（翻译与审查并行：边翻译边审查，审查通过才写回/显示） ----------

    async def _dual_pipeline(self, items, skip_fn=needs_lang_value_translation, **kw) -> None:
        """通用双线翻译+审查管道（用户诉求：全流程翻译与审查并行）。

        翻译 producer 不断翻译入队，审查 consumer 攒够 batch_size（吞吐档位）条一起审查
        （用户诉求：审查攒批跟翻译同数量）→ 审查通过才写回各 item 的 sink。审查是初审；
        漏翻/重翻后仍保留原文的条目留给源终审判定（终审只审漏翻，不重复审合格译文）。
        """
        # v1.4.1 预扫描：已汉化/记忆命中的条目批量 bump stage done，进度条开局就涨到位。
        # v1.4.2 修复（用户「total 虚标 61885，实际 36740」）：预扫描跳过的条目
        # **只计 stage done，不计全局 done**——total 已改成 len(state_jobs)（待翻译缺口），
        # 预扫描跳过的不算「翻译完成」，否则 done > total。
        items_list = list(items)
        _prescan_done = 0
        _prescan_items = []
        for it in items_list:
            text = it.get("text", "")
            key = it.get("key", "")
            src_mod = it.get("mod", "")
            src_file = it.get("file", "")
            # 已汉化（含 CJK）/ 技术串 / 作者名 → 批量 bump stage done（不加全局 done）
            if (not self.same_script and
                    (not skip_fn(text, self.req.target_lang) or str(key).endswith(".author"))):
                self._skipped_n += 1
                # 修复：预扫描跳过的条目只推 stage 明细，不加全局 done——
                # total = len(state_jobs)（待翻译缺口），跳过的不属于「翻译完成」
                self._bump_stage_only()
                _prescan_done += 1
            else:
                _prescan_items.append(it)
        if _prescan_done > 0:
            self.store.save(self.state)   # 批量 bump 后立即存盘，前端轮询读到
        review_queue = asyncio.Queue(maxsize=2000)   # v1.4.4：1000 → 2000，翻译快审查慢时减少阻塞
        done_event = asyncio.Event()
        producer = asyncio.create_task(self._translate_batch_pipeline(
            _prescan_items, self._engine_translate, self._batch_size, skip_fn=skip_fn,
            enqueue_fn=review_queue.put, **kw))
        # v1.4.4：审查攒批固定40条（不跟随并发），让审查尽早开始、不攒太久
        # 原逻辑 max(20, batch_size * concurrency) 会攒到几百条，翻译完审查还没开始
        consumer = asyncio.create_task(self._review_pipeline(
            review_queue, done_event, review_batch=40))
        # 审查状态灯：审查管道活跃 → 前端红灯「静默审查中…」；全部审查完成 → 绿灯「审查完成」
        self.state.reviewing = True
        self.store.save(self.state)
        try:
            try:
                await producer
            except BaseException:
                # 修复（recheck）：producer 抛异常/被取消时 consumer 成为孤儿任务——
                # 取消并 await 收敛，避免其仍卡在审查网络请求里挂到排空退出（窗口期竞态写回）
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
                raise
            finally:
                # 修复：done_event 通知 consumer 排空退出（替代入队 sentinel——
                # 队列满 + consumer 异常时 put(None) 会永久阻塞死锁）
                done_event.set()
            await consumer
        finally:
            self.state.reviewing = False
            self.store.save(self.state)

    async def _safe_review_write(self, batch: list[dict]) -> None:
        """审查写回带兜底：写回异常不杀 consumer（修复：consumer 一旦异常退出，
        producer 在队列满时 put 会永久阻塞死锁）。审查写回失败记 warn，不影响翻译主流程。"""
        try:
            await self._review_and_write(batch)
        except Exception as exc:
            self.state.progress.append({"status": "warn", "error": f"审查写回跳过：{exc}"})
            try:
                self.store.save(self.state)
            except Exception:
                pass

    def _push_review_status(self, n: int) -> None:
        """v1.2.9：审查进度不再单独 push 计数条——done 已由审查写回时推进（_write_reviewed
        bump），读数即审查进度，无需额外「静默审查中」提示（用户诉求）。保留回调接口占位
        （_review_chunk_start_cb/done 仍维护 _active_review 计数，无显示用途）。"""
        pass

    def _review_chunk_start_cb(self, n: int) -> None:
        """审查批请求开始（v1.2.9）：在飞审查批数 +1 → 聚合递增（审查也并发 40×16）。"""
        self._active_review += 1
        self._push_review_status(n)

    def _review_chunk_done_cb(self, n: int) -> None:
        """审查批请求完成（成败都）：在飞审查批数 -1 → 聚合递减。"""
        self._active_review = max(0, self._active_review - 1)
        self._push_review_status(n)

    async def _review_pipeline(self, queue, done_event, review_batch: int = 30) -> None:
        """审查管道消费者：从翻译队列取已翻译条目，攒批审查 → 写回。

        与翻译管道并行运行（用户诉求：翻译了 1 入审查、2 在审查 1 时继续翻译……
        两个互不干涉的管道，审查是翻译的后续同步进行）。退出靠 done_event + 排空：
        producer 结束后 set done_event，consumer 排空队列退出——替代入队 sentinel
        （队列满时 put sentinel 可能永久阻塞死锁）。
        """
        batch: list[dict] = []
        while True:
            if done_event.is_set() and queue.empty():
                if batch:
                    await self._safe_review_write(batch)
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue   # 队列空且未完成：继续等 producer 入队 / done_event
            if item is None:
                break      # 兼容：显式 sentinel（None）也退出
            batch.append(item)
            if len(batch) >= review_batch:
                await self._safe_review_write(batch)
                batch = []
        if batch:
            await self._safe_review_write(batch)

    async def _review_and_write(self, batch: list[dict]) -> None:
        """三级审查流水线（用户诉求）：
        初审 → 过：写最终产物；没过：重审（forced 重翻）→ 翻出译文（≠原文）：写最终产物；
        仍原文（==source 漏翻）：终审（判 failed / 合理保留提示）。

        初审过审的条目不再重复审（防平白消耗 token）；审查自身故障不中断（放行当通过）。
        """
        if self.state.cancelled:
            return
        await self._wait_if_paused()
        # 审查前分流「合理保留」（用户反馈：报告一堆「审查不通过」误杀）：
        # AI 翻译时保留原文（译文==原文）且原文是**确定翻不动的**（纯占位符/代码标识）→
        # **不送 AI 审查**（审查 AI 会重复判「漏翻」→ 反复重翻 → 误报失败）。
        # 翻译阶段 keep_original_ok 已接受这种保留；这里按原文规则预判合理保留，
        # 直接写回（_write_reviewed 记录保留决策），省审查 token 且不误杀。
        # **修复（recheck）**：排除 request_failed 条目——请求失败回原文的条目必须先
        # 走下方 failed 计数，不能被「合理保留」提前摘除绕过 failed（顺序 bug）。
        for it in list(batch):
            if (not it.get("request_failed")
                    and it.get("translated") == it["source"]
                    and _is_legit_keep_by_source(it["source"])):
                self._write_reviewed(it, it["source"])
                batch.remove(it)
        if not batch:
            self.memory.save()
            self.store.save(self.state)
            return
        pairs = [{"key": it["key"], "source": it["source"], "translated": it["translated"]}
                 for it in batch]
        issues: list[dict] = []
        try:
            issues = await review_translations(self.engine, pairs, self.req.target_lang,
                                               silly_mode=self.silly,
                                               on_batch_start=self._review_chunk_start_cb,
                                               on_batch_done=self._review_chunk_done_cb)
        except Exception:
            issues = []   # 审查故障不误伤不中断：初审视为全部通过
        # 初审：不合格 key → 重审；其余 → 写最终产物
        bad_keys = {iss["key"]: iss.get("reason", "") for iss in issues}
        ok_items = [it for it in batch if it["key"] not in bad_keys and not it.get("request_failed")]
        # 修复（recheck #2）：request_failed 条目已在上方计 failed + 写回原文，
        # 必须排除出 bad_items——否则审查判「漏翻」（translated==source）进重翻闭环 → 终审再计 failed（双计）。
        bad_items = [it for it in batch if it["key"] in bad_keys and not it.get("request_failed")]
        # 修复：请求失败回原文的条目，不因审查 issues=[] 放行假成功——直接计 failed 写回原文
        for it in batch:
            if it.get("request_failed"):
                sink = it.get("sink")
                if sink is None:
                    sink = it["sink"] = self.by_mod.setdefault(it.get("modid", ""), {})
                sink[it["key"]] = it["source"]
                # v1.2.8 修复：记录失败 key，漏翻闭环据此排除（请求/服务失败重翻无意义）
                self._req_fail_keys.add((it.get("modid", ""), it["key"]))
                self.state.failed += 1
                self.failures.append({"text": it.get("source", "")[:50],
                                      "reason": "翻译服务失败（请求异常）"})
        # ok_items（审查通过）：
        #   - 真译文（translated != source）→ 写回 + 标签「审查过关」
        #   - 保留原文：**规则** _is_legit_keep_by_source 确认合理保留（纯占位符/无字母/代码标识）
        #     → 写回 + 标签「审查过关·合理保留」；
        #     规则判定「该翻的纯英文」→ **不放行**（规则兜底，不靠 AI/审查放行），并入强制重翻——
        #     用户实测：审查 AI 倾向把「该翻的界面文本」放行成纯英文，规则兜底才能拦下。
        force_retrans: list[dict] = []
        for it in ok_items:
            if it["translated"] == it["source"]:
                if _is_legit_keep_by_source(it["source"]):
                    sink = it.get("sink")
                    if sink is None:
                        sink = it["sink"] = self.by_mod.setdefault(it.get("modid", ""), {})
                    sink[it["key"]] = it["source"]
                    self._write_reviewed(it, it["source"])
                else:
                    force_retrans.append(it)
            else:
                # 修复（recheck）：审查通过的译文也过**目标语言关**——审查故障（issues=[]）
                # 时 translated≠source 的假译文（AI 只改大小写/措辞，仍是纯英文）会被当
                # 「过」放行进产物。非目标语言且非合理保留 → 并入强制重翻。
                # v1.3.0 再修（用户「大段英文连着还判过了」）：_is_target_lang 只要含 1 个
                # 汉字就判「目标语言」——AI 在长段里插几个中文词、大量英文残留的「中英混杂」
                # 被放行写回产物。补 _has_english_leak 拦截（≥3 英文词 → 进强制重翻），
                # 与重翻/终审的英文残留防线对齐，杜绝大段英文写进汉化产物。
                if (self._is_target_lang(it["translated"], self.req.target_lang)
                        and not self._has_english_leak(it["translated"])) \
                        or _is_legit_keep_by_source(it["source"]):
                    self._write_reviewed(it, it["translated"])
                else:
                    force_retrans.append(it)
        # 审查后处理闭环（用户诉求）：审查不合格（bad_items）+ 审查通过但纯英文该翻（force_retrans）。
        # —— 第一轮：**相同 prompt 重跑一遍**（不直接 forced，可能是批量/偶发偷懒或截断）：
        #    结果不同且全量中文 → 算过采用；仍原文/英文 → 进强制翻译。
        # 审查后处理闭环（v1.2.3 裁剪：串行往返 6+N → 4）。合并原「R1 相同 prompt 重跑
        # (forced=False) + R2 forced」为**一轮 forced**——forced=True 是超集（追加「宁可
        # 翻译不要保留」，仍豁免专名/命令/代码标识），对不稳定 API 少一轮串行请求的收益
        # 大于原「同 prompt 重跑」的边际收益。
        retrans_items = bad_items + force_retrans
        _final: list[dict] = []
        if retrans_items:
            _net = False
            try:
                _t = [it["source"] for it in retrans_items]
                _rs = [bad_keys.get(it["key"], "译文质量不合格，请翻译成目标语言")
                       for it in retrans_items]
                _g, _m = await self._engine_translate(_t, _rs, forced=True)
                _mf = (_m or {}).get("failed") or set()
                _mk = (_m or {}).get("kind") or "other"
                _net = (_mk in ("timeout", "network", "ratelimit", "server")
                        or any(x in _mf for x in _t))
            except Exception:
                _g = [it["source"] for it in retrans_items]
                _net = True   # 重翻异常：按网络失败处理
            if _net:
                # 网络/服务失败：初次有真译文（目标语言）保留写回；否则进终审
                # v1.3.0 再修：初次译文也查英文残留——大段英文 + 零星中文词不保留，进终审
                for it, tr in zip(retrans_items, _g):
                    if it["translated"] != it["source"] \
                            and self._is_target_lang(it["translated"], self.req.target_lang) \
                            and not self._has_english_leak(it["translated"]):
                        self._write_reviewed(it, it["translated"])
                    else:
                        _final.append(it)
            else:
                # v1.2.7 轻量化：forced 重翻后**不再 AI 再审**（省 1 次 API 往返，对慢 API
                # 每批审查省掉整轮请求）——规则终验分流：翻出译文且是目标语言且无英文残留
                # → 写回；仍原文/纯英文/中英混杂 → 交由 _final_judge_batch 终审。
                # 英文残留用 _has_english_leak 兜底（原 AI 再审抓中英混杂，裁剪后规则补上）。
                for it, tr in zip(retrans_items, _g):
                    if tr != it["source"] and self._is_target_lang(tr, self.req.target_lang) \
                            and not self._has_english_leak(tr):
                        self._write_reviewed(it, tr)
                    else:
                        _final.append(it)                 # 仍原文/英文/混杂 → 终审（规则终验）
            # 终审批量（v1.2.3）：一次 forced 批量终审（替代原逐条 _final_judge_leak 的 N 次单发）
            if _final:
                await self._final_judge_batch(_final, bad_keys)
        self.memory.save()
        self.store.save(self.state)

    def _write_reviewed(self, it: dict, translated: str) -> None:
        """审查通过：名称归一化（关键步骤）→ 写最终产物（sink，lang=by_mod[modid]，
        json/pack=文本源 out dict）+ 记忆 + 明细。"""
        translated = clean_surrogates(translated)   # 重翻结果也可能含 surrogate，写盘前统一清理
        # 修复（recheck）：占位符一致性防御——译文丢失/改写占位符（%s/%d/§a/{var}/{{...}}）
        # 时**不落盘坏译文**（游戏内显示错乱/崩），回退原文（占位符正确）+ 计 failed 提示。
        # 触发场景：protect 标记被 LLM 丢弃导致 restore 找不回，或 AI 删改占位符。
        # 占位符校验在归一化**前**做——归一化登记的规范译名视为已审查可信。
        src_full = it.get("source") or ""
        if translated != src_full and not validate_placeholders(src_full, translated):
            sink = it.get("sink")
            if sink is None:
                sink = it["sink"] = self.by_mod.setdefault(it.get("modid", ""), {})
            sink[it["key"]] = src_full
            self.state.failed += 1
            self.failures.append({"text": src_full[:50],
                                  "reason": "译文丢失或改写了占位符，已保留原文"})
            self._legit_kept.add((it.get("modid", ""), it["key"]))
            self.state.progress.append({"key": it["key"], "source": src_full,
                                        "translated": src_full, "status": "error",
                                        "mod": it["modid"], "reviewed": True})
            return
        # 名称归一化（审查通过的关键步骤）：第一个目标语言译文登记为规范译名，
        # 后续与规范译名不一致的（AI 自由发挥/并发竞态）在此覆盖统一——审查通过
        # 的前提是「译名一致」。归一化后的译名写 memory → 后续条目命中直接沿用。
        translated = self._apply_name_norm(it.get("source") or "", translated)
        # v1.1.0：移除 _apply_term_override 词级术语覆盖（light→灯 全替换破坏语境的元凶；
        # 多语境统一交给 AI 语境归一化审查 _ai_contextual_normalize 判定）
        # 修复：sink 用 None 判断（依赖空 dict falsy 脆弱，Agent 审查确认）
        sink = it.get("sink")
        if sink is None:
            sink = it["sink"] = self.by_mod.setdefault(it.get("modid", ""), {})
        sink[it["key"]] = translated
        # 一致性统计（兜底归一化用）：记录「原文→译名」出现次数
        self._record_consistency(it["source"], translated)
        if translated != it["source"]:
            self.memory.set(it["source"], self.req.target_lang, translated)
            # 项目级专有名词对照：译名写入词汇表（Zeno→泽诺），后续同词（含变体）统一沿用
            self._add_project_term(it["source"], translated)
        else:
            # AI 保留原文（初审通过视为合理保留）→ 记录「保留」决策 + 标记，
            # 漏翻兜底 _retry_remaining_leaks 不再重复审查这批（Agent 审查确认重复审）
            self._legit_kept.add((it.get("modid", ""), it["key"]))
            self._add_project_term(it["source"], it["source"])
        # 标签「审查过关」（reviewed=True）：审查通过且写入的条目标注已过关——语义上区分
        # 「已审查确认」与「未审查/重翻中」，前端可据此展示「✓ 审查通过」
        self.state.progress.append({"key": it["key"], "source": it["source"],
                                    "translated": translated, "status": "done",
                                    "mod": it["modid"], "reviewed": True})
        # v1.2.9 用户诉求：翻译 done 计数**只在审查过关写回时增加**（翻译阶段 enqueue 不再
        # 提前 bump）——读数 = 已过审成品，翻译与审查对齐，审查完一批读数同步进下一轮。
        # 修复（recheck 续联漏计）：统一 _bump_stage——_write_reviewed 的条目都是**本次新
        # 翻译/重翻**（skip/记忆命中走 pipeline 其他分支用 _bump_stage_only，基准已含），
        # 续联时新翻译条目也应加全局 done，不能只加 stage 明细。
        # v1.3.7 账本幂等：带 (归属, key) 唯一键——记忆命中/简繁/主翻译入队审查管道的
        # 条目在此统一 bump（提前 bump 已删），账本保证同 key 只计一次（防任何残留双计）
        self._bump_stage(key=self._progress_key(it.get("modid", ""), it.get("file", ""), it.get("key", "")))

    def _add_project_term(self, source: str, decision: str) -> None:
        """项目级专有名词对照记录（用户诉求：Zeno→泽诺 这类决策写词汇表，后续统一沿用）。

        决策是译名（Zeno→泽诺）或「保留原文」（translated==source，如 mod 名）。
        记录后动态重建 engine.glossary_prompt——后续批次的 prompt 携带该对照，AI 对同一
        专有名词（含变体如 Zeno's Sword）保持统一译名/保留，避免一词多译。
        持久化靠 memory（译名已写；保留在终审也写），下次任务 extract_terms 重新提取。
        """
        source = (source or "").strip()
        if not source or len(source) > 24 or len(decision or "") > 24:
            return   # 太长的不是专有名词，跳过（防 prompt 膨胀）
        self.project_terms[source] = decision
        if isinstance(self.engine, LLMClient):
            # 修复（recheck）：不能整体替换 engine.glossary_prompt——原逻辑用只有 project_terms
            # 的 term_inject_prompt 覆盖，用户预填术语表（glossary.json）+ 记忆提取的基础术语
            # 全被冲掉，后续批次专有名词一致性失效（Zeno→泽诺 这类决策丢 prompt）。改为在
            # 基础术语之上**追加**项目动态术语（基础固定不膨胀，动态部分最多 30 条）。
            terms = dict(sorted(self.project_terms.items(), key=lambda kv: len(kv[0]))[:30])
            inject = term_inject_prompt(terms)
            base = self.glossary_prompt or ""
            self.engine.glossary_prompt = f"{base}\n\n{inject}" if base else inject

    def _is_target_lang(self, text: str, lang: str) -> bool:
        """判断译文是否属于目标语言（登记规范译名的前提：第一个必须翻成对应语言，
        不能保留原文/翻错语言）。zh → 含汉字；ja → 含假名；ko → 含谚文；
        其他语言无脚本可判 → 宽松放行（交由审查质量把关）。"""
        text = (text or "").strip()
        if not text:
            return False
        if lang in ("zh_cn", "zh_tw"):
            return any('一' <= ch <= '鿿' for ch in text)
        if lang == "ja_jp":
            return any(('぀' <= ch <= 'ゟ') or ('゠' <= ch <= 'ヿ') for ch in text)
        if lang == "ko_kr":
            return any('가' <= ch <= '힯' for ch in text)
        return True

    def _has_english_leak(self, text: str) -> bool:
        """英文残留检测（v1.2.7+ 审查裁剪补防线）：译文含 ≥3 个英文单词（非纯代码/路径/
        命令/占位符保留形态）→ 判为英文残留（整段 / 中英混杂）。

        原 AI 再审会抓「中英混杂」，裁剪后由本规则兜底——命中则不写回产物，
        交 _final_judge_batch 再翻 / 合理保留 / 记失败，杜绝「大段英文写进汉化产物」。
        单个/两个专名（Xaero、AE2）不误伤（<3 个英文词）。
        """
        if not text or _is_legit_keep_by_source(text):
            return False
        if re.search(r"[/\\:]", text):
            # v1.3.1 修复（用户 AE2 教程 markdown 大段英文判过）：原「含 / 或 : 就豁免」太宽——
            # 长 markdown 链接路径 (.../ae2-mechanics/subnetworks.md) 含斜杠被误判「路径」→
            # 大段英文逃逸写回。只有**整段是纯路径/命令/命名空间形态**才豁免：
            #   纯路径（无空格、/ 分隔）| / 开头的命令 | namespace:key（无空格）
            t = text.strip()
            if (re.fullmatch(r"(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+", t)
                    or t.startswith("/")
                    or re.fullmatch(r"[A-Za-z0-9_.-]+:[A-Za-z0-9_.-]+", t)):
                return False
        return len(re.findall(r"\b[A-Za-z]{3,}\b", text)) >= 3

    async def _prebuild_terms(self, texts: list[str]) -> None:
        """翻译前预扫描术语表（DocuTranslate 模式，用户诉求「全部优化」）：
        统计待翻译文本中的高频英文词 → AI 批量统一译名 → 预填 _norm_terms +
        glossary_prompt。让 AI **一开始**就遵循统一译名，而非翻译中发现才登记
        （避免第一批错译/多译——Zeno 无论在哪都是泽诺，从头到尾一致）。

        仅 LLM 引擎、高频词 ≥3 个时启用；失败静默（不阻塞翻译）。

        v1.4.6：同步正则扫描几万条文本阻塞事件循环（用户「卡在正在翻译语言文件」元凶之一），
        扫描段用 to_thread 丢后台线程 + 整体 wait_for 60s 超时，不阻塞心跳/任务状态。
        """
        from collections import Counter
        try:
            def _scan_terms(texts: list[str]) -> list[str]:
                """同步统计高频词（在后台线程跑，避免阻塞事件循环）。"""
                cnt: Counter = Counter()
                for t in texts:
                    if not t:
                        continue
                    for w in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,23}", t):
                        wl = w.lower()
                        if wl in {"the", "and", "for", "with", "you", "your", "this", "that",
                                  "from", "into", "when", "will", "can", "has", "have", "are",
                                  "was", "not", "but", "all", "any", "per", "via"}:
                            continue
                        if w.isupper() and len(w) <= 3:
                            continue   # 缩写（RF/HP）跳过
                        # 过滤代码标识：下划线（player_name）/驼峰（ModelViewMat 中部大写）/
                        # 含数字（iron2）→ 不参与预扫描（防误计代码词）
                        if "_" in w or re.search(r"[A-Z]", w[1:]) or re.search(r"[0-9]", w):
                            continue
                        cnt[w] += 1
                # 按**频率**排序取前 40（修复：原 list(freq) 是首次出现顺序，长尾词挤掉真高频）
                return [w for w in sorted(cnt, key=cnt.get, reverse=True)
                        if cnt[w] >= 3 and len(w) <= 20][:40]

            top = await asyncio.wait_for(
                asyncio.to_thread(_scan_terms, texts), timeout=20.0)
            if len(top) < 3:
                return
            _r, _m = await self._engine_translate(top, None, forced=False)
            for w, tr in zip(top, _r):
                tr = (tr or "").strip()
                # 已有规范译名（CFPA 人工权威 / 记忆 / 既有登记）→ 不覆盖（AI 单译名不得推翻权威）
                if w in self._norm_terms:
                    continue
                # 单词级术语译名以格助词结尾（Rune→「符文的」）→ 剥掉助词（→符文）再登记。
                # 修复：带「的」的译名只适合完整句子语境，作词级术语会让 AI 在「X of the Y」
                # 结构里再叠一个「的」→「强化符文的的宝珠」（用户实测 Reinforced Rune of the
                # Orb→强化符文的的宝珠）。剥成空/单字（刃的→刃）→ 无独立名词义，丢弃不登记。
                stripped = tr.rstrip("的地得之了")
                if stripped != tr and len(stripped) < 2:
                    continue
                tr = stripped
                # v1.1.0：只登记**形态像专有名词**的词（_is_proper_noun）——light/right/iron
                # 等小写常用词即使高频也不登记为规范译名（否则 glossary 注入 light→灯，
                # light blue 变灯蓝色），杜绝机械统一破坏语境
                if _is_proper_noun(w) and tr and tr != w \
                        and self._is_target_lang(tr, self.req.target_lang) \
                        and not re.search(r"[A-Za-z]{3,}", tr):
                    self._norm_terms[w] = tr
            if self._norm_terms:
                terms = dict(sorted(self._norm_terms.items(), key=lambda kv: len(kv[0]))[:30])
                inject = term_inject_prompt(terms)
                base = self.glossary_prompt or ""
                self.glossary_prompt = f"{base}\n\n{inject}" if base else inject
                self.engine.glossary_prompt = self.glossary_prompt
        except Exception:
            pass   # 预扫描失败不阻塞翻译

    def _apply_name_norm(self, source: str, translated: str) -> str:
        """专有名词规范译名登记（v1.1.0 重构）。

        **移除「第一定义强制覆盖」**——旧版第一个译文成为规范译名后强制覆盖后续同原文：
        right 被翻成「右面」后所有 right（正确/权利/右边）全被覆盖成「右面」，破坏语境。
        改为：
        - 只对**形态像专有名词**的原文（_is_proper_noun：Zeno/Iron Ingot 等）登记规范译名，
          供 AI 语境归一化审查（_ai_contextual_normalize）build 前判定语境后参考；
        - **不强制覆盖**当前译文——AI 按语境自由翻译，多译文冲突由 AI 审查判定是否统一；
        - 常用词（light/right/iron）形态不像专名 → 不登记、不干预，绝不做机械统一。"""
        src = (source or "").strip()
        if not src:
            return translated
        # 非专名形态 → 不干预（用户核心诉求：常用词绝不机械统一）
        if not _is_proper_noun(src):
            return translated
        # 译文是目标语言（真译名）→ **只在无规范译名时**登记（第一个确认的为规范译名，
        # 后续不覆盖——规范译名保持稳定，多译文冲突由 AI 语境归一化审查判定）。不强制覆盖。
        if (translated != src and src not in self._norm_terms
                and self._is_target_lang(translated, self.req.target_lang)):
            self._norm_terms[src] = strip_particle(translated)   # 规范译名剥助词（符文的→符文）
        return translated

    # v1.1.0：_apply_term_override（词级术语覆盖）已删除——它是「light 翻成灯后 light blue
    # 变灯蓝色」的机械元凶；多语境统一改由 AI 语境归一化审查（_ai_contextual_normalize）判定。

    def _record_consistency(self, source: str, translated: str) -> None:
        """一致性统计累加：记录「原文→译名」出现次数（收尾归一化选主译名用）。

        在所有写回真译文/审查通过的汇聚点调用。保留原文（translated==source）也记录，
        归一化选主译名时会排除它（保留原文常是漏翻/未处理，不配当主译名）。
        """
        source = (source or "").strip()
        translated = (translated or "").strip()
        if not source or not translated:
            return
        stat = self._consistency_stats.setdefault(source, {})
        stat[translated] = stat.get(translated, 0) + 1

    def _iter_translation_mappings(self):
        """产出全部「key→译文」映射（归一化替换遍历用）：
        by_mod（语言文件）/ json-lines / pack / 硬编码 mapping。"""
        yield from self.by_mod.values()
        for srcs in self.json_lines_translations.values():
            for _src, out in srcs:
                yield out
        for _src, out in self.pack_translations:
            yield out
        yield from self.hard_mappings.values()

    # v1.1.0：_consistency_normalize（机械硬统一）已删除——它把「right 翻成右面后所有
    # right 全替换成右面」，破坏语境；归一化改由 _ai_contextual_normalize（AI 语境判定：
    # 只对专名 + 同语境的候选，AI 在语境中重翻统一，常用词绝不机械统一）。

    def _collect_norm_candidates(self) -> list[dict]:
        """收集「同原文 ≥2 个不同译文」且形态像专名的候选组（AI 语境归一化候选）。
        只收 _is_proper_noun 通过（首字母大写/驼峰/命名式）——light/right 小写常用词
        天然不进，绝不机械统一（用户核心诉求）。"""
        out = []
        for source, variants in self._consistency_stats.items():
            if len(variants) < 2:
                continue                 # 单译名/无冲突 → 不触发（用户确认：仅多译文冲突时）
            if not _is_proper_noun(source):
                continue                 # 小写常用词不进候选
            out.append({"source": source,
                        "variants": sorted(variants, key=variants.get, reverse=True)})
        return out

    async def _ai_judge_normalization(self, cands: list[dict]) -> dict[int, dict]:
        """AI 批量判定候选组：是否专名、各译文语境是否相同、应统一的给规范译名。
        返回 {索引: {"should_unify": bool, "canonical": str}}；机翻引擎/请求失败返回空
        （宁可不统一，不机械破坏语境）。解析兼容「统一 泽诺」「统一：泽诺」等输出
        （Agent 审查：原正则要求空格才捕获 canonical，冒号/无空格被静默丢弃）。"""
        if not isinstance(self.engine, LLMClient):
            return {}
        lines = "\n".join(
            f"[i{i}] 原文「{c['source']}」出现译文：{'、'.join(c['variants'])}"
            for i, c in enumerate(cands))
        llm = self.engine
        client = llm._get_client()
        body = {
            "model": llm.model,
            "messages": [
                {"role": "system", "content": _NORM_JUDGE_SYSTEM},
                {"role": "user", "content": lines},
            ],
            "temperature": 0.0,
            "max_tokens": 1024,
        }
        try:
            resp = await client.post(f"{llm.base_url}/chat/completions", json=body)
            resp.raise_for_status()
            out = resp.json()["choices"][0]["message"]["content"]
        except Exception:
            return {}
        judged: dict[int, dict] = {}
        for line in out.splitlines():
            m = re.match(r"\[i(\d+)\]\s*(统一|不统一)\s*[:：]?\s*(\S+)?", line.strip())
            if m:
                idx = int(m.group(1))
                if m.group(2) == "统一":
                    judged[idx] = {"should_unify": True,
                                   "canonical": (m.group(3) or "").strip("（）()「」")}
                else:
                    judged[idx] = {"should_unify": False}
        return judged

    async def _ai_renormalize(self, cands: list[dict], judged: dict[int, dict]) -> None:
        """对 AI 判定「应统一」的分组统一全部产物数据源：
        - by_mod（有 source 对照）：AI 在语境中重翻（feedback 带规范译名提示），
          带 modid 收集（修复 Agent 审查：同 key 多 mod 只翻第一个）；重翻后仍 == 旧变体
          的收敛为 canonical（AI 已判同语境，定向替换安全）。
        - 其余源（json-lines/pack/硬编码，无 source 对照）：按 AI 判定同语境的旧译名
          定向替换 canonical（修复 Agent 审查：跨映射不一致漏统一）。"""
        unified: list[tuple[str, str, list[str]]] = []   # [(source, canonical, old_variants)]
        for idx, j in judged.items():
            if idx < 0 or idx >= len(cands):
                continue                     # 索引越界防崩（Agent 审查：越界 → 全部静默失败）
            if not j.get("should_unify") or not j.get("canonical"):
                continue
            source = cands[idx]["source"]
            canonical = j["canonical"]
            old = [v for v in cands[idx]["variants"] if v not in (canonical, source)]
            if not old:
                continue
            unified.append((source, canonical, old))
        if not unified:
            return
        # 1) by_mod：AI 语境重翻（带 modid 收集，不走 _collect_retry_items 的 key 去重/break）
        retry_items: list[dict] = []
        for source, canonical, _old in unified:
            fb = f"「{source}」是专有名词且各出现语境相同，规范译名应为「{canonical}」，请重翻为自然中文"
            for modid, entries in self.by_mod.items():
                src = self.source_by_mod.get(modid, {})
                for key, trans in entries.items():
                    if src.get(key) == source and trans != canonical:
                        retry_items.append({"key": key, "text": source, "sink": entries,
                                            "modid": modid, "reason": fb})
        if retry_items:
            # keep_original_ok=False（修复 recheck 🔴）：归一化重翻的条目原本有译文，若 AI
            # forced 重翻返回原文（source），keep_original_ok=True 会把原文写回 sink 覆盖
            # 已有译文 → 产物英文残留且收敛循环不恢复。这里拒绝原文写回（宁保留旧译文）。
            await self._translate_batch_pipeline(
                retry_items,
                lambda texts, _reasons=None: self._engine_translate(texts, _reasons, forced=True),
                batch_size=5, skip_fn=lambda t: False,
                force_engine=True, count_done=False, keep_original_ok=False)
            # 收敛：AI 重翻后仍 == 旧变体（未遵循提示）→ 定向替换 canonical（同语境安全）
            for source, canonical, old in unified:
                for modid, entries in self.by_mod.items():
                    src = self.source_by_mod.get(modid, {})
                    for key, trans in entries.items():
                        if src.get(key) == source and trans in old:
                            entries[key] = canonical
        # 2) 非 by_mod 源（json-lines/pack/硬编码）：无 source 对照 → 按 AI 判定同语境的
        #    旧译名定向替换 canonical（修复 Agent 审查：跨映射不一致漏统一）
        for mapping in self._iter_translation_mappings():
            if any(mapping is m for m in self.by_mod.values()):
                continue                      # by_mod 已重翻，跳过
            for key, v in list(mapping.items()):
                for _s, canonical, old in unified:
                    if v in old:
                        mapping[key] = canonical
                        break

    async def _ai_contextual_normalize(self) -> None:
        """AI 语境归一化（v1.1.0 重构，替代 _consistency_normalize 机械硬统一）：
        收集「同原文 ≥2 个译文」的**专名形态**候选 → AI 判定语境是否相同 → 同语境
        AI 在语境中重翻统一；不同语境/常用词保留不动。只 LLM 引擎生效（机翻无 AI
        判定能力 → 跳过不统一）。判定后同步 _norm_terms 与 memory（Agent 审查：
        旧 _consistency_normalize 统一后写 memory，新流程不能断）。"""
        if not self._consistency_stats:
            return
        cands = self._collect_norm_candidates()
        if not cands:
            return
        judged = await self._ai_judge_normalization(cands)
        if not judged:
            return
        await self._ai_renormalize(cands, judged)
        # 同步判定结果：规范译名落 _norm_terms（以 AI 判定为准，修复「首个登记次优」冲突）
        # + memory（续联/后续阶段复用统一译名）
        _mem_changed = False
        for idx, j in judged.items():
            if 0 <= idx < len(cands) and j.get("should_unify") and j.get("canonical"):
                src, canonical = cands[idx]["source"], j["canonical"]
                if self._norm_terms.get(src) != canonical:
                    self._norm_terms[src] = canonical
                if self.memory.get(src, self.req.target_lang) != canonical:
                    self.memory.set(src, self.req.target_lang, canonical)
                    _mem_changed = True
        if _mem_changed:
            self.memory.save()

    async def _final_judge_batch(self, items: list[dict], bad_keys: dict) -> None:
        """终审批量（v1.2.3 性能修复）：N 条漏翻**一次** forced 批量终审，替代原
        _final_judge_leak 逐条单发（慢 API 下每条一个请求 → 串行放大）。落败路径
        （合理保留不 failed / 该翻没翻记 failed + 记忆/词汇表）逐条照搬原语义。"""
        if not items:
            return
        _srcs = [it.get("source") or "" for it in items]
        _reasons = [bad_keys.get(it["key"], "译文质量不合格") for it in items]
        _g: list[str] = []
        _net = False
        try:
            _g, _m = await self._engine_translate(_srcs, _reasons, forced=True)
            _mf = (_m or {}).get("failed") or set()
            _mk = (_m or {}).get("kind") or "other"
            # v1.3.4 修复（Agent recheck）：_net 只反映「整批网络/服务错误」——原 `any(x in _mf)`
            # 让**单条失败**设整批 _net=True → 首循环 `not _net` 守卫把同批成功译文也弃写原文、
            # _still 也不记账。去掉 any，单条失败（_mf 精确到文本）由逐条逻辑处理。
            _net = _mk in ("timeout", "network", "ratelimit", "server")
        except Exception:
            _g = list(_srcs)
            _net = True
        _still: list[tuple[dict, str]] = []
        for it, tr in zip(items, _g):
            src_full = it.get("source") or ""
            src = src_full[:50]
            reason = (bad_keys.get(it["key"], "") or "").strip() or "译文质量不合格"
            sink = it.get("sink")
            if sink is None:
                sink = it["sink"] = self.by_mod.setdefault(it.get("modid", ""), {})
            # 尽力输出：翻出非原文、目标语言、且无英文残留的译文 → 输出写回（覆盖率优先）。
            # _has_english_leak 拦截中英混杂/整段英文残留（原 AI 再审防线，裁剪后规则兜底）
            # v1.3.4（Agent recheck）：去掉 `not _net` 守卫——_net 只反映整批网络错误，
            # 单条失败（_mf）不再拖累；tr 有效（目标语言）就写回，成功译文不被弃
            if tr and tr != src_full \
                    and self._is_target_lang(tr, self.req.target_lang) \
                    and not self._has_english_leak(tr):
                self._write_reviewed(it, tr)
                continue
            # v1.4.1（用户「质量差就差点，但要保证有中文翻译」）：forced 重翻没出中文时，
            # 检查**初翻是否有中文**——有则用初翻的中文译文，不记 failed。
            # 初翻结果在 it["translated"] 里（审查打回时保留了原译文）。宁可要质量差的
            # 中文翻译，也不要记 failed 导致没翻译（用户核心诉求）。
            _orig_tr = it.get("translated") or ""
            if _orig_tr and _orig_tr != src_full \
                    and self._is_target_lang(_orig_tr, self.req.target_lang):
                self._write_reviewed(it, _orig_tr)
                continue
            sink[it["key"]] = src_full
            # 合理保留必须**原文规则为主**（v1.3.9 修复「翻译出原文」根因）：
            # `_is_legit_keep_by_source(原文)` 已按原文形态判定——含空格/实词的句子（如
            # FTB 描述 "Placement rule is the same as for RF Amplifier: it has to touch at
            # least 2 Fusion Casing/Glass blocks."）**该翻**（返回 False）。
            # 原 `or _is_legit_keep(reason)` 让 AI 审查措辞单独放行：reason 说「含资源路径
            # Casing/Glass 不宜翻译」命中「资源路径」词 → 判合理保留 → **原文写记忆污染**，
            # 后续同文本永久原文（用户反复「翻译出原文」元凶）。现在**必须原文规则先确认
            # 是技术串**（_is_legit_keep_by_source 返回 True），reason 只作**补充佐证**——
            # 原文规则说该翻的，reason 含任何词都不放行，进二次重试（forced 再翻一次）。
            if _is_legit_keep_by_source(src_full):
                self.failures.append({"text": src, "reason": f"保留原文：{reason}"})
                self.memory.set(src_full, self.req.target_lang, src_full)
                self._add_project_term(src_full, src_full)
            elif _is_legit_keep(reason) and len(src_full) <= 40:
                # 仅当原文短（≤40 字符，形如专名/命令/单 token）+ AI reason 确认技术类别
                # 才判保留——长文本描述（>40）绝不因 reason 放行（防止长句被误判保留）
                self.failures.append({"text": src, "reason": f"保留原文：{reason}"})
                self.memory.set(src_full, self.req.target_lang, src_full)
                self._add_project_term(src_full, src_full)
            else:
                # v1.3.3 优化（用户「AE2 教程长 markdown 明明有内容却没法汉化」）：
                # 该翻没翻 → 收集进二次重试（不立即记 failed）——长 markdown AI 整批返回
                # 原文偷懒，二次批量重试（forced 更强指令）给最后机会翻出，仍失败才记 failed
                _still.append((it, reason))
        # v1.3.4（Agent recheck）：_still 分支不能因 _net 丢失记账——
        # _net（整批网络/服务失败）时二次重试大概率也失败，直接记 failed（不再白等）；
        # 否则二次批量重试（forced 更强指令）给最后机会翻出，仍失败才记 failed。
        if _still:
            if _net:
                for it, reason in _still:
                    src_full = it.get("source") or ""
                    src = src_full[:50]
                    sink = it.get("sink")
                    if sink is None:
                        sink = it["sink"] = self.by_mod.setdefault(it.get("modid", ""), {})
                    self.state.failed += 1
                    self.failures.append({"text": src, "reason": f"漏翻未译出：{reason}"})
            else:
                _again_srcs = [it["source"] for it, _ in _still]
                _again_reasons = [r for _, r in _still]
                try:
                    _g2, _m2 = await self._engine_translate(_again_srcs, _again_reasons, forced=True)
                except Exception:
                    _g2 = list(_again_srcs)
                for (it, reason), tr2 in zip(_still, _g2):
                    src_full = it.get("source") or ""
                    src = src_full[:50]
                    sink = it.get("sink")
                    if sink is None:
                        sink = it["sink"] = self.by_mod.setdefault(it.get("modid", ""), {})
                    if tr2 and tr2 != src_full \
                            and self._is_target_lang(tr2, self.req.target_lang) \
                            and not self._has_english_leak(tr2):
                        self._write_reviewed(it, tr2)
                    else:
                        self.state.failed += 1
                        self.failures.append({"text": src, "reason": f"漏翻未译出：{reason}"})

    async def _final_judge_leak(self, it: dict, reason: str = "") -> None:
        """终审单条（v1.2.3 保留为薄包装，兼容外部调用）：委托 _final_judge_batch。"""
        await self._final_judge_batch([it], {it["key"]: reason})

    def _save_report(self) -> None:
        """任务收尾生成翻译报告（通用所有模式：整合包/mod/光影；地图由 maps/flow 单独生成）。

        报告数据落盘产物区 outputs/<task_id>/report.json（含**全部**未翻译条目，不只前端
        显示的 60 条），前端任务完成点「阅读翻译报告」弹窗阅读（不下载）。
        """
        try:
            from app.report import build_report, save_report
        except Exception:
            return
        products = []
        # 修复（recheck）：modpack 的 jar 内 json/lines 写回 jar（组装区 hardcoded/）已打进
        # 整合包汉化.zip，不是独立产物——过滤掉，报告只列独立交付（zip / modjar 顶层 jar）。
        # 用户实测报告里出现多个 mod 汉化 jar 误当独立产物。
        _build_root = self.work_dir / "build" / self.task_id
        for fp in self.exported:
            try:
                f = Path(fp)
                if _build_root in f.parents:
                    continue   # 组装区内产物只进 zip，不单独列报告
                size_mb = round(f.stat().st_size / 1048576, 1) if f.exists() else 0.0
            except OSError:
                size_mb = 0.0
            name = Path(fp).name
            desc = ("汉化资源包/补丁包" if str(fp).endswith(".zip")
                    else "汉化 jar" if str(fp).endswith(".jar") else "翻译产物")
            products.append({"name": name, "desc": desc, "size_mb": size_mb})
        report = build_report(
            input_name=self.state.display_name,
            target_lang=self.req.target_lang,
            total=self.state.total, done=self.state.done, failed=self.state.failed,
            # 覆盖率分母扣减：跳过翻译量（技术串 skip + 硬编码 AI 判定非用户可见）
            skipped=self._skipped_n + sum(len(v) for v in self.hard_excluded_by_jar.values()),
            stages=[{"name": s.get("name", ""), "total": s.get("total", 0),
                     "done": s.get("done", 0)} for s in self.state.stages],
            products=products,
            failures=self.failures,
        )
        save_report(report, self.outputs_dir, self.task_id)

    def _save_progress(self) -> None:
        """保存项目进度（断点续联）：done/total/stage/名称/原始输入路径 落盘
        progress/<项目id>.json，下次拖入同一项目提示「已断点续联（上次 X%）」，
        命中记忆继续翻译不重复；前端任务列表启动时扫描 progress/ 显示未完成项目。
        任务被「删除项目」取消（.deleted 标记）→ 跳过保存，防删除后任务收尾重建缓存。"""
        # 修复（recheck）：run() 早期异常时 project_id 未计算（初始化 ""）——避免写出
        # progress/.json（list_projects 会读到空 id 的「未完成项目」）
        if not self.project_id:
            return
        p = self.work_dir / "progress" / f"{self.project_id}.json"
        # 用户诉求：删除项目（含进行中/取消的任务）后缓存要真正消失——任务被删除项目
        # 取消时 finally 仍会走到这里，检查 .deleted 标记存在则不再重建 progress。
        # 标记「消费式」删除：这是最后一次保存机会（finally 只调一次），消费掉标记防残留
        marker = p.parent / f"{self.project_id}.deleted"
        if marker.exists():
            # 修复（recheck #3）：标记改**非消费式**——_save_progress 现被批末节流（≥2s）调用，
            # 消费式 unlink 会让下一次节流调用正常写 progress → 已删除项目的缓存「复活」。
            # 标记保留：存在即跳过写；重新翻译同项目时 run() 会清除该标记。
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        # 用户诉求：**任务完成（产物已生成）→ done 写满 total（100%）**——即使实际
        # done<total（漏翻/合理保留占计数），产物已交付即视为完成，关闭重开不再显示
        # 「可断点续联」；只有真正中断（cancelled/failed 未出产物）才保留实际进度续联。
        _done = self.state.total if self.state.status == "done" else self.state.done
        # 修复：display_name 为空（中断在取名步骤前）→ 用原始输入文件名，
        # 续联列表显示真实文件名而非 5a818a7428e7 哈希指纹（用户实测）
        _name = self.state.display_name or Path(str(self.req.path or "")).name or ""
        p.write_text(json.dumps({
            "name": _name,
            # 原始输入路径（续联用：项目列表点「续联」→ autoTranslate(path) 重算指纹匹配）
            "path": str(self.req.path or ""),
            "done": _done, "total": self.state.total,
            "failed": self.state.failed,
            "stage": self.state.stage,
            # 修复（用户实测）：build 阶段卡住取消时翻译已完成（done==total）但产物未生成——
            # 列表误判「已完成」不算未完成 → 无续联按钮。存 status，list_projects 用
            # status 判断「未完成」（非 done 状态即使 done==total 也可续联）
            "status": self.state.status,
            "updated": time.time(),
        }, ensure_ascii=False), encoding="utf-8")

    # ---------- 阶段 1：语言文件 + 审计闭环 ----------

    async def _stage_lang(self) -> None:
        """语言文件 jobs → 批量翻译（含 effect 的 AI 自主判断 + CFPA 保护）→ 审计强制重翻闭环。

        阶段 1 临时关闭引擎技术串过滤（语言文件值是可翻译文本，键才是标识符），
        结束后立即恢复（阶段 2/3 的 json/硬编码仍走技术串过滤）。
        """
        # 阶段预设提示（用户诉求：状态提示按阶段内置预设，不是只有「解压」一条就完事）
        await self._smart_status("正在翻译语言文件…")
        # 记忆续翻提示（用户选半自动续翻）：memory.json 持久化已翻译词条，关闭重开后重新开始，
        # 命中记忆的词条直接跳过不调 AI。统计命中数给用户明确「在续翻」。
        if self.state_jobs:
            try:
                _hits = sum(1 for job in self.state_jobs
                            if self.memory.get(job.source_text, self.req.target_lang))
                if _hits:
                    self.state.progress.append({"status": "done", "key": "记忆续翻",
                                                "source": f"命中 {_hits} 条",
                                                "translated": "已翻译词条直接跳过，不重复调用 AI"})
                    self.store.save(self.state)
            except Exception:
                pass   # 统计失败不影响翻译
        # 语言文件值是可翻译文本（键才是标识符）：关闭引擎技术串过滤 + 跳过函数只判已汉化，
        # 让 "Requires_Armor" 这类 snake_case 真实短语不被 should_translate 误杀。
        self.engine_filter_technical = getattr(self.engine, "filter_technical", None)
        if self.engine_filter_technical is not None:
            self.engine.filter_technical = False
        # 语言文件条目收集：effect.* 效果名不再一刀切英文（Xaero 审查修复——一刀切让
        # 大部分 mod 效果名不翻译，体验差）。改为交给 AI 翻译并自主判断：prompt 指示
        # 「会拼资源位置 Identifier 的效果名保留英文、纯显示效果名翻译」，AI 语义判断
        # 替代脆弱规则。AI 故意保留原文不计 failed（keep_original_ok）。
        lang_items = []
        effect_items = []
        for job in self.state_jobs:
            if job.key.startswith("effect."):
                effect_items.append({"key": job.key, "text": job.source_text,
                                     "sink": self.by_mod.setdefault(job.modid, {}),
                                     "mod": job.modid})
                continue
            # CFPA 社区人工词库精确命中（modid + key）→ 直接写回，零成本人工翻译
            cfpa_hit = self.cfpa["by_key"].get(f"{job.modid}\x00{job.key}") if self.cfpa else None
            if cfpa_hit:
                # 修复（recheck）：zh_tw 目标时 CFPA 词库是简体中文——写入繁体产物前繁化
                #（否则简体直接泄入 zh_tw.json 且被登记为规范译名，繁体产物混简体）
                if self.req.target_lang == "zh_tw":
                    try:
                        cfpa_hit = traditional(cfpa_hit)
                    except Exception:
                        pass
                # 名称归一化：CFPA 是社区人工权威译名，直接登记为规范译名——后续同原文
                # 走 AI 的条目（其他 mod/文件）审查时统一跟随 CFPA 译名，不自由发挥。
                if (cfpa_hit != job.source_text
                        and self._is_target_lang(cfpa_hit, self.req.target_lang)):
                    self._norm_terms[job.source_text] = strip_particle(cfpa_hit)   # 剥助词
                    # 记录一致性（修复 Agent 审查 🔴2）：CFPA 权威译名进 _consistency_stats，
                    # 否则同 source 走 AI 翻出不同译名时，候选收集 len(variants)<2 跳过，
                    # CFPA「泽诺」+ AI「泽昂」并存且无兜底
                    self._record_consistency(job.source_text, cfpa_hit)
                self.by_mod.setdefault(job.modid, {})[job.key] = cfpa_hit
                self.memory.set(job.source_text, self.req.target_lang, cfpa_hit)   # 写记忆，json/硬编码阶段同文本复用
                # 续联：CFPA 命中只推进 stage 明细（全局基准已含）
                # v1.3.7 账本幂等：CFPA 直接写回不进审查管道，账本保证不与其他路径双计
                self._bump_stage(key=self._progress_key(job.modid, "", job.key)) \
                    if not self._resume else self._bump_stage_only()
                self.state.progress.append({"key": job.key, "source": job.source_text,
                                            "translated": cfpa_hit, "status": "done",
                                            "mod": job.modid})
                continue
            lang_items.append({"key": job.key, "text": job.source_text,
                               "sink": self.by_mod.setdefault(job.modid, {}),
                               "mod": job.modid})
        # A: 翻译前预扫描术语表（DocuTranslate 模式）——AI 先统一高频词译名，
        # 预填 _norm_terms + glossary_prompt，让翻译一开始就遵循统一译名
        if isinstance(self.engine, LLMClient) and lang_items:
            await self._prebuild_terms([it["text"] for it in lang_items])
        try:
            if isinstance(self.engine, LLMClient):
                # 翻译与审查并行（用户诉求）：翻译管道不断翻译入队，审查管道并行审查写回——
                # 翻译了 1 入审查、2 在审查 1 时继续翻译……两个互不干涉的管道，审查通过才显示。
                # 都吃吞吐档位：翻译并发走 pipeline 批次，审查并发走 review_translations 分页。
                # v1.4.0 修复（用户「Bombs/Plantkillable 小词没法正常翻译」）：
                # keep_original_ok=False——语言文件值是用户可见文本，AI 保留原文不该自动放行，
                # 必须送审查判断。原来默认 True + _has_english_leak 阈值≥3词才拦 → 单个英文
                # 短词（Bombs 1个词）被当「AI 故意保留」放行写回产物（不翻译）。
                # 改 False 后：AI 返回原文 → 记 failed → 送审查 → 审查抓「漏翻」→ 强制重翻。
                # 专有名词（Minecraft/Xaero）审查会判合理保留，不误伤。
                await self._dual_pipeline(lang_items, skip_fn=needs_lang_value_translation,
                                          keep_original_ok=False)
            else:
                # 机翻/兜底：无 AI 审查能力，直接翻译写回（原逻辑，终审审计兜底）
                await self._translate_batch_pipeline(
                    lang_items, self._engine_translate,
                    self._batch_size, skip_fn=needs_lang_value_translation)
            # effect 单独跑：AI 判断保留原文（拼 Identifier 防崩）不算翻译失败
            if effect_items:
                await self._translate_batch_pipeline(
                    effect_items, self._engine_translate,
                    self._batch_size, skip_fn=needs_lang_value_translation, keep_original_ok=True)
        finally:
            # 阶段 1 结束立即恢复：阶段 2/3 的结构化 JSON / 硬编码仍走技术串过滤
            if self.engine_filter_technical is not None:
                self.engine.filter_technical = self.engine_filter_technical

        # 终审（按文本源=modid 收官，用户诉求：终审是每个项目最后的一次审查，不是最后的大阶段）：
        # 只审「初审没翻出的漏翻条目」（translated==source）——合理保留提示、该翻记 failed；
        # 初审过审的译文不进终审（防平白消耗 token）。漏翻专项重翻兜底先跑（单条再翻一次）。
        if self.by_mod:
            self.source_by_mod = {}
            for job in self.state_jobs:
                self.source_by_mod.setdefault(job.modid, {})[job.key] = job.source_text
            await self._retry_remaining_leaks()
            self._report_term_audit()
            await self._record_residual_failures()   # 规则审计兜底（AI 判定已由三级终审完成）

    # ---------- 统一质量审查 + 强制重翻闭环（AI 裁判核心） ----------

    def _collect_retry_items(self, keys: set[str], reasons: dict[str, str] | None = None) -> list[dict]:
        """按 key 收集重翻条目（定位源文本 + by_mod sink），未命中的跳过。

        reasons：审查不合格原因 {key: reason}——随条目携带，重翻 prompt 注入 feedback，
        让 AI 针对原因修正翻译到合格（用户诉求：审查要修复不是仅提出）。
        """
        reasons = reasons or {}
        items = []
        for key in keys:
            for modid, entries in self.by_mod.items():
                src = self.source_by_mod.get(modid, {})
                if key in entries and key in src:
                    items.append({"key": key, "text": src[key], "sink": entries,
                                  "reason": reasons.get(key, ""), "mod": modid})
                    break
        return items

    async def _retry_remaining_leaks(self) -> None:
        """漏翻专项兜底（用户诉求：先认真审查判定「这条是不是能翻译的文本」，再专注翻译）。

        流程：
        1. 收集 by_mod 里译文仍 == 原文的漏翻条目（排除 effect.* 防资源定位崩溃）
        2. **先审查**这些漏翻条目，判定每条「该翻的界面文本」还是「合理保留」
           （专有名词/命令/代码标识——审查规则 5 对专有名词保留不判漏翻）
        3. 只对「该翻没翻」的条目逐条强制再翻（batch_size=1 专注单条最可能翻出）；
           合理保留的不重翻（AI 保留正确，重翻浪费 token 且可能误翻专有名词）
        仍翻不出的留给 _record_residual_failures 精准判 failed。
        """
        leak_pairs = []
        for modid, entries in self.by_mod.items():
            src = self.source_by_mod.get(modid, {})
            for key, trans in entries.items():
                s = src.get(key)
                if (s and trans == s and not str(key).startswith("effect.")
                        and (modid, key) not in self._legit_kept
                        and (modid, key) not in self._req_fail_keys):   # v1.2.8：请求失败不重翻
                    leak_pairs.append({"key": key, "source": s, "translated": trans})
        if not leak_pairs:
            return
        # 先审查漏翻条目：审查规则对「专有名词保留」不判漏翻，只标记「该翻没翻」的
        issues = []
        _review_failed = False
        try:
            issues = await review_translations(self.engine, leak_pairs, self.req.target_lang,
                                               on_batch_start=self._review_chunk_start_cb,
                                               on_batch_done=self._review_chunk_done_cb)
        except Exception:
            issues = []
            _review_failed = True   # 修复（recheck）：审查故障时不判合理保留，全部残留进重翻——
                                    # 原逻辑 issues=[] → retry_keys 空 → 残留纯英文失去最后单条
                                    # 重翻机会。重翻内部有 _net_fail 保护（网络失败不误杀好译文）
        if _review_failed:
            retry_keys = {lp["key"] for lp in leak_pairs}
        else:
            retry_keys = {iss["key"] for iss in issues
                          if not _is_legit_keep(iss.get("reason", ""))}
        if not retry_keys:
            return
        reasons = {k: "上次审查判漏翻：这是用户可见的界面文本，必须翻译出来" for k in retry_keys}
        retry_items = self._collect_retry_items(retry_keys, reasons)
        if not retry_items:
            return
        # 漏翻专项重翻（审查静默：不输出审查过程提示）
        # batch_size=1 逐条：模型专注单条、配合「宁可翻译不要保留」最强指令，最可能翻出
        await self._translate_batch_pipeline(
            retry_items,
            lambda texts, _reasons=None: self._engine_translate(texts, _reasons, forced=True),
            batch_size=1, skip_fn=needs_lang_value_translation,
            force_engine=True, count_done=False)

    async def _record_residual_failures(self) -> None:
        """终审规则兜底：AI 判定已由审查管道三级终审（_final_judge_leak）完成，
        这里只做规则审计——审计不变量 error（缺译文/占位符）→ 记 failed。
        修复（Agent 审查）：流水线真失败条目（failures 已含 key）因未写 sink 被审计
        「缺少中文译文」再计一次 → failed 虚高；跳过已记录 key 消除双计。"""
        _failed_keys = {f.get("key") for f in self.failures if f.get("key")}
        for modid, entries in self.by_mod.items():
            src = self.source_by_mod.get(modid, {})
            for issue in audit_invariants(src, entries):
                if issue["severity"] == "error" and issue["key"] not in _failed_keys \
                        and (modid, issue["key"]) not in self._req_fail_keys:
                    # v1.2.9（Agent recheck）：request_failed 条目的 failures 记录不带 key 字段
                    #（_failed_keys 含不到），其键名命中 audit 语义规则时被再计 failed → 双计；
                    # _req_fail_keys 精确记录 request_failed 的 (modid,key)，审计跳过
                    self.state.failed += 1
                    self.failures.append({"text": src.get(issue["key"], "")[:50],
                                          "reason": f"审计不通过：{issue['message']}"})

    def _report_term_audit(self) -> None:
        """术语/键名/重复译法审计：只提示不重翻（语义规则，需人工在校对台确认）。"""
        audit_errors, audit_warnings = audit_translation(self.by_mod, self.req.target_lang,
                                                         self.source_by_mod)
        if audit_errors:
            self.state.failed += len(audit_errors)
            self.state.progress.append({"status": "warn",
                                        "error": f"术语审计 {len(audit_errors)} 条不合规"
                                                 f"（官方术语/占位符/键名语义，请在校对台确认）"})
        for w in audit_warnings:
            self.state.progress.append({"status": "warn", "key": w["key"], "error": w["message"]})

    # ---------- 阶段 2：json/lines 全文本覆盖 + 整合包目录文本源 ----------

    async def _stage_json(self) -> None:
        """结构化 JSON / en_us 文本写回 jar 副本（阶段 json）+ 整合包目录文本源（阶段 pack）。

        双线（用户诉求）：翻译+审查并行（_dual_pipeline），审查攒批=翻译批次；每文本源
        翻译审查完立即源终审（_final_review_pairs 只审漏翻），不再是最后的全量大阶段。
        """
        self._set_stage("json")
        await self._smart_status("正在翻译配置/文本…")
        json_items: list[dict] = []
        json_sources: list[tuple[Path, TextSource, dict[str, str]]] = []
        for jar, srcs in self.text_sources_by_jar.items():
            for src in srcs:
                out: dict[str, str] = {}
                for key, text in src.entries.items():
                    json_items.append({"key": key, "text": text, "sink": out,
                                       "file": src.source_path, "mod": ""})
                json_sources.append((jar, src, out))
        if isinstance(self.engine, LLMClient):
            await self._dual_pipeline(json_items, skip_fn=needs_translation)
        else:
            await self._translate_batch_pipeline(json_items, self._engine_translate, self._batch_size)
        # 每文本源：归集产物 + 源终审（只审漏翻，初审过审不重审）
        for jar, src, out in json_sources:
            if out:
                self.json_lines_translations.setdefault(jar, []).append((src, out))

        self._set_stage("pack")
        await self._smart_status("正在翻译整合包目录文本…")
        pack_items: list[dict] = []
        pack_sources: list[tuple[TextSource, dict[str, str]]] = []
        if self.kind == "modpack" and self.pack_sources:
            for src in self.pack_sources:
                out: dict[str, str] = {}
                for key, text in src.entries.items():
                    pack_items.append({"key": key, "text": text, "sink": out,
                                       "file": src.source_path, "mod": ""})
                pack_sources.append((src, out))
        if isinstance(self.engine, LLMClient):
            await self._dual_pipeline(pack_items, skip_fn=needs_translation)
        else:
            await self._translate_batch_pipeline(pack_items, self._engine_translate, self._batch_size)
        for src, out in pack_sources:
            if out:
                self.pack_translations.append((src, out))

    # ---------- 阶段 3：硬编码（引擎分流） ----------

    async def _stage_hardcode(self) -> None:
        """硬编码翻译：LLM 引擎 AI 判断是否用户可见；machine 无此阶段；兜底引擎批量全翻。"""
        if not self.engine_machine:
            self._set_stage("hardcode")
        if isinstance(self.engine, LLMClient):
            # LLM 引擎：AI 判断「是否用户可见」并翻译（批量）
            # v1.4.6 优化：合并所有 jar 的候选**一次批量判断**——原逐 jar 串行调用
            # ai_judge_translate，jar 候选少时每 jar 只发 1 批请求，几十个 jar 排队串行
            # （用户「排着队在翻译」）。合并后所有 jar 候选一起判断，ai_judge_translate
            # 内部批次 gather 并行 + 并发信号量补位，jar 之间不再串行等待。
            # 逐批推进进度：ai_judge 每判断完一批回调，进度条实时涨（用户反馈
            # 「硬编码时进度条不涨」——不能等全部判断完才一次性 done）
            def _judge_start(n: int) -> None:
                self.state.progress.append({"status": "translating", "count": n,
                                            "note": "AI 判断硬编码"})
                self.store.save(self.state)
            def _judge_done(n: int) -> None:
                # 批完成：逐批推进 done（续联不重复加）
                self._bump_stage(n) if not self._resume else self._bump_stage_only(n)
                self.store.save(self.state)
            # 第一遍：收集所有 jar 的 fresh 候选 + 缓存命中
            # 省 token（用户诉求）：硬编码字符串跨 jar 重复率极高，判断过写记忆后续复用
            _cached_by_jar: dict = {}          # jar -> {text: trans}
            _excluded_cached_by_jar: dict = {}  # jar -> [text]
            _all_fresh: list[dict] = []          # 候选带 jar 标记
            _cached_n = 0
            for jar, cands in self.hard_candidates_by_jar.items():
                if self.state.cancelled:
                    self.state.status = "cancelled"
                    self.store.save(self.state)
                    self._aborted = True
                    return
                await self._wait_if_paused()
                for c in cands:
                    c_text = c["text"]
                    cached = self.memory.get(c_text, self.req.target_lang)
                    if cached and cached != c_text:
                        _cached_by_jar.setdefault(jar, {})[c_text] = cached   # 复用译文
                        _cached_n += 1
                    elif cached == c_text:
                        _excluded_cached_by_jar.setdefault(jar, []).append(c_text)  # 排除标记
                        _cached_n += 1
                    else:
                        _fc = dict(c)
                        _fc["jar"] = jar
                        _all_fresh.append(_fc)
            if _cached_n:
                # 缓存命中推进 done（续联不重复加）
                self._bump_stage(_cached_n) if not self._resume else self._bump_stage_only(_cached_n)
            # 一次批量判断（内部批次 gather 并行 + 并发补位）
            try:
                judged = await ai_judge_translate(self.engine, _all_fresh, self.req.target_lang,
                                                  known_translations=dict(sorted(
                                                      self.project_terms.items(),
                                                      key=lambda kv: len(kv[0]))[:30]),
                                                  on_batch_start=_judge_start,
                                                  on_batch_done=_judge_done,
                                                  silly_mode=self.silly)
            except Exception as exc:
                # 失败 → fresh 候选计入 failed（build 阶段 total 修正兜底）
                self.state.failed += len(_all_fresh)
                self.state.progress.append({"status": "warn",
                                            "error": f"AI 判断硬编码失败：{exc}"})
                judged = None
            # 判断期间可能被取消（ai_judge 批回调置取消）→ 拦截，不分配结果、状态保持 cancelled
            if self.state.cancelled:
                self.state.status = "cancelled"
                self.store.save(self.state)
                self._aborted = True
                return
            # 第二遍：按 jar 分配判断结果写回
            if judged is not None:
                if isinstance(judged, dict):
                    mapping_all = dict(judged)
                    unresolved_all: list[str] = []
                    excluded_all: list[str] = []
                else:
                    mapping_all = judged.translations
                    unresolved_all = judged.unresolved
                    excluded_all = judged.excluded
                for jar, cands in self.hard_candidates_by_jar.items():
                    cand_texts = {c["text"] for c in cands}
                    mapping = dict(_cached_by_jar.get(jar, {}))
                    for t in cand_texts:
                        if t in mapping_all:
                            mapping[t] = mapping_all[t]
                    if mapping:
                        # 清理译文无效 surrogate（写 jar utf-8 崩溃根因）
                        mapping = {t: clean_surrogates(tr) for t, tr in mapping.items()}
                        self.hard_mappings[jar] = mapping
                        # AI 判断译文写回记忆，后续语言文件/其他 jar 同串直接命中
                        for text, trans in mapping.items():
                            trans = self._apply_name_norm(text, trans)
                            mapping[text] = trans
                            self.memory.set(text, self.req.target_lang, trans)
                            self._record_consistency(text, trans)
                    excluded = [t for t in excluded_all if t in cand_texts]
                    excluded += _excluded_cached_by_jar.get(jar, [])
                    if excluded:
                        self.hard_excluded_by_jar.setdefault(jar, []).extend(excluded)
                        # 排除决策写记忆（原文标记）：后续 jar 同文本命中直接跳过
                        for t in excluded:
                            if self.memory.get(t, self.req.target_lang) != t:
                                self.memory.set(t, self.req.target_lang, t)
                    unresolved = [t for t in unresolved_all if t in cand_texts]
                    skipped = max(0, len(cands) - len(mapping) - len(unresolved))
                    if skipped > 0:
                        self.state.progress.append({"status": "warn",
                                                    "error": (f"{jar.name}: {skipped} 条硬编码被判定"
                                                              f"非用户可见或历史已排除，已跳过")})
                    if unresolved:
                        preview = "、".join(unresolved[:5]) + ("…" if len(unresolved) > 5 else "")
                        self.state.progress.append({"status": "warn",
                                                    "error": (f"{jar.name}: {len(unresolved)} 条硬编码判断不明确"
                                                              f"（已保持原文不翻译，只翻判断准的）：{preview}")})
                        for u in unresolved:
                            self.failures.append({"text": u[:50],
                                                  "reason": "保留原文：AI 判断不明确（模棱两可），选择不翻译"})
                        self.hard_unresolved_by_jar[jar] = unresolved
                    # 汇总 progress（judged/visible 便于前端展示）
                    self.state.progress.append({"jar": jar.name, "judged": len(cands),
                                                "visible": len(mapping), "unresolved": len(unresolved),
                                                "status": "done"})
                    if self.state.done % 10 == 0:
                        self.memory.save()
                        self.store.save(self.state)
        elif not self.engine_machine:
            # 兜底引擎（测试假引擎等）：硬编码批量全翻（无 AI 判断，复用批量流水线）。
            # total 已在初始计算含硬编码候选数（_translate_batch_pipeline 按条目推进 done）。
            for jar, cands in self.hard_candidates_by_jar.items():
                mapping: dict[str, str] = {}
                hard_items = ({"key": c["text"], "text": c["text"], "sink": mapping}
                              for c in cands)
                await self._translate_batch_pipeline(
                    hard_items, self._engine_translate,
                    self._batch_size)
                if mapping:
                    self.hard_mappings[jar] = mapping

        self.memory.save()

        # 硬编码账本（classLedger 思想）：打包前校验每个 jar 候选是否全部处置。
        # mapping（translate）+ excluded（AI 明确排除）之外的候选 = 未处置 → 兜底拦截。
        # 正常逻辑下 unresolved 已默认翻译进 mapping、exclude 明确排除，此处防漏防回归。
        if self.hard_candidates_by_jar:
            for jar, cands in self.hard_candidates_by_jar.items():
                # 仅对 AI 判断成功处理过的 jar 做账本校验；异常 jar（无 excluded 记录）
                # 已在外层 except 计 failed，这里跳过防双计。
                if jar not in self.hard_excluded_by_jar:
                    continue
                mapping = self.hard_mappings.get(jar, {})
                excluded_set = set(self.hard_excluded_by_jar.get(jar, []))
                # 修复：判断不明确「选择不翻译」的候选视为已处置（严格策略，不算未处置失败）
                unresolved_set = set(self.hard_unresolved_by_jar.get(jar, []))
                uncovered = [c["text"] for c in cands
                             if c["text"] not in mapping
                             and c["text"] not in excluded_set
                             and c["text"] not in unresolved_set]
                if uncovered:
                    self.state.failed += len(uncovered)
                    self.state.progress.append({"status": "warn",
                                                "error": (f"{jar.name}: {len(uncovered)} 条硬编码未处置"
                                                          f"（未翻译且未明确排除），已跳过")})

        if self.state.failed > 0:
            self.state.progress.append({"status": "warn",
                                        "error": f"{self.state.failed} 条翻译失败（具体原因见流程结束后翻译报告）"})

    # ---------- 阶段 build：产物组织 ----------

    async def _stage_build(self) -> None:
        """产物组织（outputs/<task_id>/ 下）：modjar → 单一汉化 jar；modpack → 资源包 + 补丁包 + 硬编码方案。

        每个产物单位经 verify_translated_jar 审查门禁（zip 完整 + 语言文件 + Identifier），
        不过不输出（防交付即崩）。原 jar 只读铁律：先 copy2 副本再改。
        """
        # 打包阶段预设提示（写回 jar/资源包/补丁包可能耗时）
        await self._smart_status("正在打包产物…")
        # pack_format：按真实 MC 版本自动识别注入（材质包兼容——infer_pack_format 从已有
        # pack.mcmeta/lang 后缀猜，.json 一律回退 15，翻译 1.21.9 等新版整合包会写出 1.20.1
        # 的格式导致对应版本游戏拒绝加载，用户实测「生成的材质包不兼容」）。detect_mc_version
        # 多来源识别：整合包 manifest（pack.toml/manifest.json/mmc-pack.json/instance.json）→
        # mods 元数据 → 单 jar 元数据；识别到 → version_to_pack_format 映射注入正确格式，
        # 识别不到才回退 infer_pack_format（语言后缀 / 已存在 pack.mcmeta）。
        # 修复（recheck）：infer_pack_format/detect_mc_version 内部遍历 mods jar 读元数据，
        # 大整合包几百个 jar 同步遍历会阻塞事件循环（进度/SSE 冻结）→ to_thread
        pack_format = await asyncio.to_thread(infer_pack_format, self.path)
        _mc = None
        if self.kind == "modpack":
            _mc = await asyncio.to_thread(detect_mc_version, self.path)
        elif self.kind == "modjar" and self.jars:
            _mc = await asyncio.to_thread(detect_mc_version, self.jars[0])
        if _mc:
            pack_format = version_to_pack_format(_mc)
        # 1.21.9+（25w31a 起）：pack_format 为 major.minor 数组，pack.mcmeta 用
        # min_format/max_format（resourcepack.pack_mcmeta 处理）；语言后缀判断仍用整数。
        pack_spec = pack_format_spec(_mc) if _mc else pack_format
        # 材质包描述（pack.mcmeta description）：{整合包名}全量{语言}化 · 覆盖率{xx}%。
        # 之前固定 "MC Auto Translator"（用户实测资源包列表显示 auto translate 英文），
        # 改为项目名 + 全量{对应语言}化（zh_cn→简体中文化、zh_tw→繁体中文化、ja→日文化，
        # 跟随目标语言）+ 覆盖率；覆盖率 = 成功 / **可翻译量**（总文本 - 不算 failed 但跳过
        # 翻译的量：技术串 skip + 硬编码 AI 判定非用户可见）。用户诉求：跳过翻译本来就不该
        # 翻，算进分母虚低覆盖率（如 total=10000 含 2000 硬编码跳过 → 可翻译量 8000）。
        _skipped = self._skipped_n + sum(len(v) for v in self.hard_excluded_by_jar.values())
        _cov_denom = max(self.state.total - _skipped, 1)
        _cov = round(max(self.state.done - _skipped, 0) / _cov_denom * 100, 1)
        pack_desc = (f"{self.state.display_name or '整合包'}全量"
                     f"{lang_display_name(self.req.target_lang)}化 · 覆盖率{_cov}%")
        # 组装区：modpack 用 temp（散装组织后打包进 zip、cleanup 清理——产物文件夹只留
        # 打包好的成品）；modjar 直接用产物区（单个 jar 产物，不套组装区）
        build_dir = (self.work_dir / "build" / self.task_id) if self.kind == "modpack" else self.out_dir
        build_dir.mkdir(parents=True, exist_ok=True)
        if self.kind == "modjar":
            # modjar → 单一汉化 jar：语言文件 + json/lines + 硬编码全写回一个 jar 副本。
            # 命名 {中文名}-{语言}化.jar（中文名取 resolve_mod_name，取不到回退原 stem；
            # 后缀「{语言}化」支持任意目标语言）。
            for jar in self.jars:
                jar_copy = self.out_dir / friendly_output_name(jar, self.req.target_lang)
                try:
                    # 原 jar 只读铁律：先 copy2 副本再改。
                    # 修复（recheck）：modjar 分支漏 to_thread——copy2/write_lang_into_jar/
                    # write_translated 全是同步大 IO（每次 write_translated 全量解压+重打包），
                    # 大 jar（100~500MB）期间事件循环被占死、轮询/SSE/取消失效，与 modpack
                    # 分支原则不一致。
                    await asyncio.to_thread(shutil.copy2, jar, jar_copy)
                    # 语言文件写回：解压副本 → 写 assets/<modid>/lang/<target>.<ext>（合并已有 zh）→ 重打包
                    await asyncio.to_thread(write_lang_into_jar, jar_copy, self.by_mod,
                                            self.req.target_lang, pack_format)
                    # json/lines 全文本覆盖写回 jar 副本
                    for src, trans in self.json_lines_translations.get(jar, []):
                        try:
                            await asyncio.to_thread(write_translated, jar_copy, src, trans)
                        except Exception as e:
                            # 修复：单文件写失败计 failed + warn，不炸整个 build（对齐发现阶段容错，
                            # 否则一个坏编码文件让整任务 failed、全部产物丢失）
                            self.state.failed += 1
                            self.state.progress.append({"status": "warn", "key": src.source_path,
                                                        "error": f"写回失败：{e}"})
                    # 硬编码替换（同一副本）
                    mapping = self.hard_mappings.get(jar)
                    if mapping:
                        # 修复（recheck）：to_thread 防事件循环冻结（同步大 IO——解压 jar +
                        # 逐 class 解析/重写 + 重打包，运行期间 /api/task 轮询/SSE 全停）
                        result = await asyncio.to_thread(replace_hardcoded_strings, jar_copy, mapping)
                        if result["failed_classes"]:
                            # failed_classes 累加进 state.failed + warn
                            self.state.failed += len(result["failed_classes"])
                            self.state.progress.append({"status": "warn",
                                                        "error": (f"{jar.name}: {len(result['failed_classes'])} 个 class 替换失败"
                                                                  f"（已跳过保留原字节）")})
                except Exception as e:
                    # 修复（recheck）：write_lang_into_jar 之前裸调用——失败时残留损坏 jar_copy
                    # 在产物区且未进 exported，download 端点会把损坏 jar 当成品返回。改为失败
                    # 即删除半截副本 + 计 failed，不让坏产物进产物文件夹。
                    self.state.failed += 1
                    self.state.progress.append({"status": "warn", "key": jar.name,
                                                "error": f"构建汉化 jar 失败：{e}"})
                    try:
                        jar_copy.unlink(missing_ok=True)
                    except OSError:
                        pass
                    continue
                self.exported.append(str(jar_copy))
                self.hard_count += 1
                # v1.3.7：build 剥离全局进度——只推进 build stage 明细（全局 done 翻译完已到 total）
                self._bump_stage_only()
                self.store.save(self.state)
                await self._verify_one(jar, jar_copy)
        else:
            # modpack → **解压即用的「整合包汉化」结构**（用户刚需）：
            #   mods/I18nUpdateMod.jar        —— i18n 下载器 mod（进游戏自动下载 CFPA 全量汉化）
            #   resourcepacks/模组汉化资源包/   —— 本应用 AI 翻译补缺口（CFPA 未覆盖的 mod）
            #   任务书/教程/进度等补丁按整合包相对路径解压（config/ data/ 直接覆盖生效）
            #   使用说明.txt + 打包「整合包汉化.zip」（解压拷进整合包根目录即用）
            # 产物不产修改版 mod jar（无二次分发纠纷），硬编码由单 mod 模式承担。
            # 组装区已在 _stage_build 开头定义（modpack → temp build/<task_id>/）：所有散装
            # （资源包/mods/补丁/使用说明）在此组织，打完 zip 后 cleanup 整体清理——产物
            # 文件夹只留「整合包汉化.zip」（+ report.json），不再一地散装（用户诉求），更省空间。
            if self.by_mod:
                # AI 补丁资源包（解压目录结构，直接放 resourcepacks/；描述含整合包名 +
                # 全量{语言}化 + 覆盖率，游戏资源包列表直接可见）
                rp_dir = build_dir / "resourcepacks" / "模组汉化资源包"
                # 修复（recheck）：同步写几百 modid 资源包文件 → to_thread 防事件循环冻结
                await asyncio.to_thread(build_resource_pack_dir, self.by_mod, self.req.target_lang,
                                        pack_spec, rp_dir, pack_desc)
                self._bump_stage_only()   # v1.3.7：build 只推进 stage 明细，不 bump 全局
                self.store.save(self.state)
            # 内置 i18n mod（i18n 是 mod 应放 mods 文件夹；进游戏自动下载 CFPA 全量汉化）
            _i18n = bundled_i18n_jar()
            if _i18n:
                mods_dir = build_dir / "mods"
                mods_dir.mkdir(parents=True, exist_ok=True)
                try:
                    await asyncio.to_thread(shutil.copy2, _i18n, mods_dir / _i18n.name)
                except OSError:
                    pass
            # 补丁条目：目录文本源译文（按整合包相对路径组织，解压即覆盖生效）。
            # 修复（recheck）：FTB Quests 新格式（1.20+）——quests/lang/*.snbt 翻译后**生成目标
            # 语言文件 lang/<target_lang>.snbt**（FTB 2100.1.0+ 按语言代码加载 lang/*.snbt），
            # 不覆盖 en_us.snbt；其余源覆盖原路径。
            patch_entries: list[tuple[str, str | bytes]] = []
            for src, trans in self.pack_translations:
                # 修复（用户实测 FTB 任务/KubeJS 物品未汉化）：用 **target_path**（en_us → zh_cn）
                # 而非 source_path——原逻辑把译文写回 en_us 原路径，游戏按目标语言读 zh_cn
                # 永远看不到译文（KubeJS 物品 lang、FTB 任务翻译键 ftbquestlocalizer lang 全失效）。
                # snbt 无 en_us 段 target==source → 覆盖原 snbt（正文汉化），不受影响。
                rel = src.target_path
                if "/quests/lang/" in rel and rel.endswith(".snbt"):
                    rel = rel.rsplit("/", 1)[0] + f"/{self.req.target_lang}.snbt"
                patch_entries.append((rel, render_pack_source(src, trans, self.path)))
            # 硬编码：VP 方案（内置 VP all jar + 映射进补丁包，离线可用）。
            # VP 获取失败 → 提示用户自行下载 VP mod 放入 mods/（不产 hardcoded 修改版
            # jar——用户刚需：整合包硬编码用 VP 形式生效，改字节码是二次分发纠纷）
            vp_used = False
            if self.hard_mappings:
                vp_pairs: dict[str, str] = {}
                for mapping in self.hard_mappings.values():
                    vp_pairs.update(mapping)
                # 修复（recheck）：VP **内置 all jar 跨 loader/版本通用**——直接优先用内置，
                # **零遍历 mods jar、零网络**（用户质疑：为下载一个通用 VP 去遍历推断 loader
                # 纯属多余）。仅当内置缺失（异常打包/frozen 漏收数据）才兜底推断 loader +
                # 在线下载（此时遍历 + 联网不可避免）。
                loader, mc_version = "内置", "通用"   # 内置 all jar 跨 loader/版本通用；仅兜底下载时被覆盖
                vp_bytes = None
                bundled = bundled_vp_jar()
                if bundled:
                    try:
                        _bd = bundled.read_bytes()
                        if _bd[:2] == b"PK":
                            vp_bytes = _bd
                    except OSError:
                        pass
                if vp_bytes is None:
                    await self._smart_status("正在获取 Vault Patcher 硬编码汉化模组…")
                    loader, mc_version = await asyncio.to_thread(
                        infer_modpack_runtime, self.path / "mods")
                    vp_bytes = await download_vault_patcher(loader, mc_version)
                if vp_bytes:
                    vp_used = True
                    patch_entries.append(("mods/vault-patcher.jar", vp_bytes))
                    patch_entries.append(("vaultpatcher/modules/mc-auto-translator.json",
                                          json.dumps(build_vp_module(vp_pairs),
                                                     ensure_ascii=False, indent=2)))
                    # 修复：VP 默认只加载 config 里 mods 列表登记的模块——产物带
                    # load_all_modules:true 配置，确保硬编码映射被自动加载（用户质疑
                    # 「产物适配 VP 没有」：不带配置映射不会被 VP 读取）
                    patch_entries.append(("config/vaultpatcher_asm/config.json",
                                          json.dumps({"modules": ["mc-auto-translator"],
                                                      "load_all_modules": True},
                                                     ensure_ascii=False, indent=2)))
                    self.state.progress.append({"status": "done", "key": "vault-patcher.jar",
                                                "source": "Vault Patcher",
                                                "translated": f"已自动下载（{loader} {mc_version}），装入 mods/ 生效"})
                else:
                    self.state.progress.append({"status": "warn",
                                                "error": "Vault Patcher 获取失败（内置缺失且联网不可用），请自行下载 VP mod（mcmod/modrinth 搜 Vault Patcher）放入 mods/，硬编码汉化才能生效"})
            # 补丁解压写入组装区（config/ data/ 结构，拷进整合包即覆盖生效）
            if patch_entries:
                for rel, content in patch_entries:
                    f = build_dir / rel
                    f.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        if isinstance(content, bytes):
                            f.write_bytes(content)
                        else:
                            f.write_text(content, encoding="utf-8")
                    except OSError:
                        pass
                self._bump_stage_only()   # v1.3.7：build 只推进 stage 明细
                self.store.save(self.state)
            # 整合包 jar 内 json/lines（教程书/进度等）**不产 hardcoded 修改版 jar**（用户刚需：
            # 整合包全走资源包 / VP / 补丁形式生效，无二次分发纠纷）。分流：
            #   assets/ 路径 → 资源包（Patchouli 等经 Minecraft 资源系统加载，资源包覆盖可靠）
            #   data/ 路径   → 补丁包（datapack 覆盖 mod 数据，解压即用）
            _jar_patched = False
            # 修复（recheck）：jar 内 json/lines 写回是逐 jar 全量解压渲染，大整合包几十个 jar
            # 可能很久——原静默循环让前端停在「VP 安装」误判卡死。加每 5 个 jar 一次进度反馈。
            _jar_list = [jar for jar in self.jars if self.json_lines_translations.get(jar)]
            for _ji, jar in enumerate(_jar_list):
                json_updates = self.json_lines_translations.get(jar)
                if not json_updates:
                    continue
                if _ji % 5 == 0 or _ji == len(_jar_list) - 1:
                    self.state.progress.append({"status": "translating", "count": 0,
                                                "note": f"正在写入 {jar.name} 教程/进度文本…（{_ji + 1}/{len(_jar_list)}）"})
                    self.store.save(self.state)
                # 修复（recheck，用户实测卡住）：一次解压 jar 批量渲染全部文本源——
                # 原 render_jar_source 逐源全量解压+删除 jar → O(n²)，大 jar（AdvancedPeripherals
                # 教程书几十个文本源）卡在「正在写入…(1/N)」长时间不动
                try:
                    rendered = await asyncio.to_thread(render_jar_sources_batch, jar, json_updates)
                except Exception as e:
                    self.state.failed += len(json_updates)
                    self.state.progress.append({"status": "warn", "key": jar.name,
                                                "error": f"渲染 jar 文本失败：{e}"})
                    continue
                for rel, content in rendered:
                    # 修复（recheck zip-slip）：rel 来自 jar 条目名，恶意条目可含 ../../ 段
                    # 逃逸 build_dir 写盘（解压层有净化，此处渲染落盘环节没有）——拼接前
                    # 拒绝绝对路径与 .. 段，对齐 _build_patch_pack 的净化逻辑
                    _rp = PurePosixPath(rel)
                    if _rp.is_absolute() or ".." in _rp.parts:
                        continue
                    # 落盘用 target_path（译文目标路径，lines 的 zh_cn/），非 source_path
                    f = (build_dir / "resourcepacks" / "模组汉化资源包" / rel
                         if rel.startswith("assets/") else build_dir / rel)
                    f.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        f.write_bytes(content.encode("utf-8"))
                    except OSError as e:
                        self.state.failed += 1
                        self.state.progress.append({"status": "warn", "key": rel,
                                                    "error": f"写补丁失败：{e}"})
                    else:
                        _jar_patched = True
            # 使用说明 + 打包「整合包汉化.zip」到产物区（散装只在组装区，zip 是唯一成品；
            # 产物文件夹 outputs/<task_id>/ 只剩成品 + report.json，用户实测不再一地散装）
            if self.by_mod or _i18n or patch_entries or _jar_patched:
                try:
                    (build_dir / "使用说明.txt").write_text(_PACKPACK_README, encoding="utf-8")
                except OSError:
                    pass
                zout = self.out_dir / "整合包汉化.zip"
                # 修复（recheck）：打包是同步大 IO（含 hardcoded jar/i18n mod 全量压缩）→
                # to_thread 防事件循环冻结；失败清理半截 zip（防 products/report 引用失真）
                def _pack_zip():
                    with zipfile.ZipFile(zout, "w", zipfile.ZIP_DEFLATED) as zf:
                        for f in sorted(build_dir.rglob("*")):
                            if f.is_file():
                                zf.write(f, f.relative_to(build_dir).as_posix())
                try:
                    await asyncio.to_thread(_pack_zip)
                except Exception as e:
                    try:
                        zout.unlink(missing_ok=True)
                    except OSError:
                        pass
                    self.state.failed += 1
                    self.state.progress.append({"status": "warn", "error": f"打包产物失败：{e}"})
                    self.store.save(self.state)
                    return
                self.exported.append(str(zout))
                self._bump_stage_only()   # v1.3.7：build 只推进 stage 明细
                self.store.save(self.state)
    async def _verify_one(self, src_jar: Path, jar_copy: Path) -> None:
        """单个汉化产物审查门禁：zip 完整 + 语言文件 + Identifier 复查，不过不输出（防交付即崩）。"""
        # 产物审查门禁：zip 完整 + 语言文件 + Identifier 复查，不过不输出（防交付即崩）。
        # 审查中/通过/失败都在进度明细可见。
        self.state.progress.append({"status": "translating", "count": 0,
                                    "note": f"审查产物 {src_jar.name}"})
        self.store.save(self.state)
        # 修复：产物审查（zip 解压复查）是同步 IO → to_thread 防阻塞事件循环
        _verdict = await asyncio.to_thread(verify_translated_jar, jar_copy, self.req.target_lang)
        # 软风险（effect 非 ASCII）→ 提示不删产物（可能是用户已有旧汉化 jar）
        if _verdict["warnings"]:
            self.state.progress.append({"status": "warn", "key": src_jar.name,
                                        "error": "审查提示：" + "；".join(_verdict["warnings"][:2])})
        if not _verdict["ok"]:
            # 硬性失败（zip 损坏/语言文件异常）→ 不输出
            jar_copy.unlink(missing_ok=True)
            self.exported.remove(str(jar_copy))
            self.state.failed += 1
            self.state.progress.append({"status": "warn",
                                        "error": (f"{src_jar.name} 审查未通过，已不输出"
                                                  f"（zip 损坏或语言文件异常）："
                                                  f"{'；'.join(_verdict['issues'][:3])}")})
        else:
            self.state.progress.append({"status": "done", "key": src_jar.name,
                                        "source": "产物审查",
                                        "translated": "通过 ✓"})

    # ---------- 阶段 shader：光影包汉化（第四模式） ----------

    async def _stage_shader(self) -> None:
        """光影包汉化：扫描 `shaders/lang/en_US.lang`（光影语言文件，key=value）→ AI 翻译 →
        产出汉化光影包（原包副本 + `shaders/lang/zh_CN.lang`）。

        **通用**（不特判任何光影）：任何含 `shaders/lang/en_US.lang` 的光影包
        （SEUS/BSL/Sildur's/Complementary/Chocapic13 等）都能汉化——游戏内切语言重载生效，
        参考 MC_ShaderTranslator / BSLShadersLang 的通用机制。原光影配置零改动（只加语言文件）。
        """
        try:
            lang_dir = self.path / "shaders" / "lang"
            src = lang_dir / "en_US.lang"
            if not src.exists():
                src = lang_dir / "en_us.lang"
            if not src.exists():
                self.state.status = "done"
                self.state.progress.append({"status": "warn",
                                            "error": "光影包缺少 shaders/lang/en_US.lang，无法汉化（该光影不支持语言切换）"})
                self.store.save(self.state)
                return
            # 解析 en_US.lang（key=value）→ 过滤可翻译值
            raw = src.read_text(encoding="utf-8")
            entries = parse_properties(raw)
            # 引擎创建（on_usage 回挂）——复用 AutoFlow 引擎初始化逻辑（与 mod 流程一致）
            self.engine = create_engine(self.cfg)
            if hasattr(self.engine, "on_usage"):
                self.engine.on_usage = self._on_usage
            self._batch_size = getattr(self.engine, "batch_size", 20)
            self.same_script = is_same_script("en_us", self.req.target_lang)
            # 进度总量：光影语言文件条目
            self.state.stages = [{"name": "lang", "total": len(entries), "done": 0}]
            self.state.total = len(entries)
            self.state.stage = "lang"
            self.store.save(self.state)
            # 光影阶段预设提示（用户诉求：光影模式也要有对应进程提示）
            await self._smart_status("正在翻译光影语言文件…")
            # 翻译（复用批量流水线；语言文件值宽松过滤，不做技术串过滤）
            translated: dict[str, str] = {}
            items = ({"key": k, "text": v, "sink": translated}
                     for k, v in entries.items() if lang_value_ok(v))
            # 修复（recheck）：走 _engine_translate（带 meta 局部 ctx）——原直接调
            # engine.translate_batch 无 meta，LLMClient 失败状态改局部 ctx 后不再写实例属性，
            # 光影模式失败返回原文被当「AI 保留」静默假成功、不记 failed、不触发网络重试。
            await self._translate_batch_pipeline(
                items, lambda texts, _reasons=None: self._engine_translate(texts, _reasons),
                self._batch_size, skip_fn=needs_lang_value_translation)
            # 产出：复制光影包目录 → 写 shaders/lang/<目标语言langcode>.lang（合并已有）→ 打 zip
            out_dir = self.outputs_dir / self.task_id
            out_dir.mkdir(parents=True, exist_ok=True)
            # 产物名用**原光影名**（原始输入文件名去扩展名）而非 self.path.name——run() 解压后
            # path 指向 extracted/<指纹>/ 缓存目录，用 self.path.name 会取到哈希指纹（用户实测
            # 光影产物名是哈希值）。后缀「{语言}化」跟随目标语言（与 mod jar 产物命名一致）。
            base = (self.state.display_name or Path(str(self.req.path)).stem
                    or self.path.name or "shaderpack").replace(" ", "_")
            pack = out_dir / base
            shutil.copytree(self.path, pack, dirs_exist_ok=True)
            # 光影语言文件命名用驼峰 langcode（en_US.lang 约定）。修复（recheck）：原写死
            # zh_CN.lang——繁体目标（zh_tw）时光影包无 zh_TW.lang，繁体玩家切语言后汉化不生效
            _langcode = _shader_langcode(self.req.target_lang)
            zh_path = pack / "shaders" / "lang" / f"{_langcode}.lang"
            existing = parse_properties(zh_path.read_text(encoding="utf-8")) if zh_path.exists() else {}
            existing.update(translated)
            zh_path.write_text(write_properties(existing), encoding="utf-8")
            # 打包汉化光影 zip（语言后缀跟随目标语言：zh_cn→简体中文化、zh_tw→繁体中文化）
            zip_out = out_dir / f"{base}-{lang_display_name(self.req.target_lang)}化.zip"
            with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in pack.rglob("*"):
                    if f.is_file():
                        zf.write(f, f.relative_to(pack).as_posix())
            self.exported.append(str(zip_out))
            self.state.status = "done"
            self.state.progress.append({"status": "done", "file": str(out_dir),
                                        "pack": str(zip_out), "hardcoded": 0})
            self.store.save(self.state)
        except Exception as e:
            self.state.status = "failed"
            self.state.progress.append({"status": "error", "error": sanitize_error(str(e))})
            self.store.save(self.state)

    # ---------- 运行中吞吐热更新 ----------

    def set_throughput(self, concurrency: int | None = None, batch_size: int | None = None) -> None:
        """运行中热更新吞吐档位（用户诉求：翻译中切换档位立即生效）。
        LLMClient 每次 translate_batch 都按全局并发池切块——改档位后**下一次批量**
        即按新值跑，无需重启任务。v1.2.8：走 engine.set_throughput（同步重建并发池），
        无该方法的老引擎（machine）退回改属性。"""
        eng = getattr(self, "engine", None)
        if eng is None:
            return
        if hasattr(eng, "set_throughput"):
            try:
                eng.set_throughput(concurrency=concurrency, batch_size=batch_size)
            except Exception:
                pass
            # 修复（recheck）：有 set_throughput（LLM/machine）就到此为止——不再 fallback
            # 直改属性，否则 machine 的并发 cap 5 会被下面的 `eng.concurrency = max(1, int())`
            # 覆盖绕过（Google 免费通道高并发全 429）
            return
        if concurrency:
            if hasattr(eng, "concurrency"):
                eng.concurrency = max(1, int(concurrency))
        if batch_size:
            bs = max(1, int(batch_size))
            self._batch_size = bs
            if hasattr(eng, "batch_size"):
                eng.batch_size = bs

    # ---------- 编排骨架 ----------

    async def run(self) -> None:
        """统一全自动翻译编排：识别 → 分派 → 扫描 → 词库 → 各阶段 → 产物 → 收尾。"""
        try:
            with _flows_lock:
                RUNNING_FLOWS[self.task_id] = self
            self.path = Path(self.req.path)
            # 输入名（去扩展名）**立即**落盘——map/shader 提前 return 走不到后面的
            # _translate_input_name，且任务在取名步骤前中断/取消时 _save_progress 的 name
            # 会是空串 → 断点续联列表回退显示哈希指纹（用户实测：名称又变 5a818a7428e7）。
            # display_name 恒为原始输入名；AI 译文进 display_name_translated，不覆盖原名。
            self.state.display_name = Path(str(self.req.path)).stem.strip() or self.path.name
            self.store.save(self.state)
            # 目录输入先下钻包裹层（用户可能直接选 zip 解压后的 xxxx/ 父目录，项目根在内）；
            # 压缩包输入是文件不受影响，解压后另有下钻。display_name 已用原始输入名，不受影响。
            self.path = unwrap_bare_wrapper(self.path)
            # 断点续联按项目分（用户诉求）：项目指纹 = 输入内容指纹（zip 中央目录 / 目录指纹），
            # 同一份整合包内容相同 → 项目 id 恒定（换路径/复制不重解压）；每个项目独立的记忆 + 进度
            # 修复：mod jar（.jar）也是 zip——统一按内容指纹（is_archive 不认 .jar，走目录指纹
            # 会退化成路径 hash → 换路径/复制即从头翻，Agent 审查确认）；目录输入才用目录指纹
            _is_arch = is_archive(self.path) or self.path.is_file()
            self.project_id = (await asyncio.to_thread(archive_fingerprint, self.path) if _is_arch
                               else await asyncio.to_thread(dir_fingerprint, self.path))
            # 任务 → 项目关联（删除项目时精确取消关联的运行中任务，不再一律 409 拒绝）
            self.state.project_id = self.project_id
            self.store.save(self.state)
            # 重新翻译本项目：清除上次「删除项目」留下的 .deleted 标记（防 _save_progress
            # 误判仍被删除 → 新任务进度不保存、断点续联失效）
            try:
                (self.work_dir / "progress" / f"{self.project_id}.deleted").unlink(missing_ok=True)
            except OSError:
                pass
            # 项目级记忆：每项目独立 memory/<项目id>.json（续联只命中本项目的词条，不跨项目污染）
            self.memory = MemoryStore(self.work_dir / "memory" / f"{self.project_id}.json")
            # 兼容迁移：项目记忆首次创建（文件不存在**或为空/损坏**）时，旧全局 memory.json
            # 有数据则并入（老版本遗留续联；修复：损坏/空文件不跳过迁移）
            _proj_mem = self.work_dir / "memory" / f"{self.project_id}.json"
            if not _proj_mem.exists() or len(self.memory.data) == 0:
                _legacy = self.work_dir / "memory.json"
                if _legacy.exists():
                    try:
                        _old = json.loads(_legacy.read_text(encoding="utf-8"))
                        if isinstance(_old, dict) and _old:
                            self.memory.data.update(_old)
                    except Exception:
                        pass
            # 修复：切到项目记忆后重建术语注入（__init__ 用全局记忆生成——续联应注入本项目
            # 记忆的专有名词对照，否则一词多译，Agent 审查确认）
            _gloss2 = load_glossary(self.work_dir / "glossary.json")
            self.base_terms = dict(_gloss2)   # 重建基础术语表（切项目记忆后）
            self.glossary_prompt = term_inject_prompt(_gloss2)
            _proj_terms = extract_terms(self.memory.data, self.req.target_lang, max_terms=150)
            self.base_terms.update(_proj_terms)
            if _proj_terms:
                _terms_str = "\n".join(f"{k} => {strip_particle(v)}" for k, v in _proj_terms.items())
                self.glossary_prompt = (self.glossary_prompt + "\n\n"
                                        "已确认术语（翻译时对应当前原文必须严格沿用对应译名，禁止一词多译）：\n"
                                        + _terms_str)
                # **修复（recheck）**：不再从 memory 预填 _norm_terms——历史劣译名（如
                # 老版本未归一化时写的 Zeno→泽昂）被预填成规范后，会强制覆盖本次 AI 翻出的
                # 更好译名（泽诺），劣译名粘性残留。规范译名由**本次任务第一个 AI 确认的
                # 对应语言译文**登记（_apply_name_norm），历史译名只作 glossary_prompt 提示
                # AI 倾向沿用；不一致由 _ai_contextual_normalize（AI 语境判定）兜底统一。
            # 项目记忆已就绪：有词条即断点续联（明确提示，不重复翻译）
            if len(self.memory.data) > 0:
                self.state.progress.append({"status": "done", "key": "断点续联",
                                            "source": f"项目记忆 {len(self.memory.data)} 条",
                                            "translated": "已就绪：命中词条跳过翻译，不重复调用 AI"})
                self.store.save(self.state)
            # 断点续联：进度基准接上 + 明确提示。
            # 优先用项目进度文件（上次 done/total，最准）；**无进度文件但项目有记忆**（旧版
            # 迁移/跨会话）→ 基准 = 记忆词条数（近似），同样续联——修复「看起来从头翻」
            _prog_file = self.work_dir / "progress" / f"{self.project_id}.json"
            _last = None
            if _prog_file.exists():
                try:
                    _last = json.loads(_prog_file.read_text(encoding="utf-8"))
                except Exception:
                    _last = None
            _mem_n = len(self.memory.data)
            # 旧版遗留：扫描 tasks/ 找历史最大 done（旧版没存项目进度文件——用户 89% 进度在旧任务里，
            # 与项目记忆同源（同一整合包），作为续联基准兜底）；同步记对应 total。
            # 修复：仅当项目有记忆（memory 迁移自同一整合包，与旧任务同源）才计入——
            # 否则新项目会继承别项目的历史 done（跨项目混淆，Agent 审查确认）
            _legacy_max, _legacy_total = 0, 0
            if _mem_n > 0:
                try:
                    _tasks_dir = self.work_dir / "tasks"
                    if _tasks_dir.is_dir():
                        for _tf in _tasks_dir.glob("*.json"):
                            try:
                                _td = json.loads(_tf.read_text(encoding="utf-8"))
                                if _td.get("done", 0) > _legacy_max and _td.get("display_name"):
                                    _legacy_max = _td["done"]
                                    _legacy_total = _td.get("total", 0)
                            except Exception:
                                pass
                except Exception:
                    pass
            if _last and _last.get("done", 0) > 0:
                # 修复：基准取「进度文件 / 记忆词条数 / 旧任务最大 done」较大值——旧版 89% 进度
                # 在旧任务里，不丢失；防新跑小进度覆盖
                _base = max(_last["done"], _mem_n, _legacy_max)
                _total_ref = _last.get("total", 0) or _legacy_total
            elif _mem_n > 0 or _legacy_max > 0:
                _base, _total_ref = max(_mem_n, _legacy_max), _legacy_total
            else:
                _base = 0
            if _base > 0:
                # 修复：done 从上次进度/记忆数开始（不是 0%），并**提前设 total**（进度文件的
                # 总数）→ 扫描/分析输入阶段进度条就直接显示基准百分比（不是 0），明确「续联中」
                self.state.done = _base
                self._resume = True
                if _total_ref:
                    self.state.total = _total_ref
                _pct = round(_base / max(self.state.total or _base, 1) * 100, 1)
                self.state.progress.append({"status": "done", "key": "断点续联",
                                            "source": f"记忆 {_mem_n} 条" + (f" · 上次进度 {_base}/{_total_ref}" if _total_ref else ""),
                                            "translated": f"已断点续联（{_pct}%）：命中记忆跳过翻译/审查，只补剩余"})
                self.store.save(self.state)
            # 修复：project_id 对 jar 也走内容指纹，但**解压只对 zip/mrpack**（jar 单文件直接处理）
            if is_archive(self.path):
                # 相同文件自动识别 + 断点重连（用户诉求）：按 zip 指纹缓存解压目录。
                # 相同整合包重新翻译直接复用缓存（不重复解压 573MB 占空间/耗时），
                # 已翻译词条另由 memory 记忆跳过。首次解压带 i/N 文件进度实时跳。
                _cache_root = self.work_dir / "extracted"
                # 指纹计算读 zip 中央目录，大文件同步调用阻塞事件循环 → to_thread
                _fp_dir = _cache_root / await asyncio.to_thread(archive_fingerprint, self.path)
                if (_fp_dir / ".done").exists():
                    # 命中解压缓存：不重新解压（断点重连）
                    self.path = _fp_dir
                    self.state.progress.append({"status": "done", "key": "解压整合包",
                                                "source": self.path.name,
                                                "translated": "命中缓存，无需重新解压"})
                    self.store.save(self.state)
                else:
                    # 关键：573MB 大整合包解压是同步阻塞，直接调用会冻住事件循环——
                    # autoTranslate 端点的 HTTP 响应发不出去，前端就一直「正在启动翻译任务」。
                    # to_thread 把解压丢进线程池，事件循环保持空闲，响应/SSE/轮询照常跑。
                    # on_progress 回调：每解压一批文件更新「正在解压整合包（i/N 个文件）」，
                    # 数据流实时跳动（用户诉求）。回调在 to_thread 线程里 save，SSE 不推但轮询能读。
                    self.state.progress.append({"status": "translating", "count": 0,
                                                "note": "正在解压整合包…"})
                    self.store.save(self.state)

                    def _on_unpack(done: int, total: int) -> None:
                        self.state.progress.append({"status": "translating", "count": 0,
                                                    "note": f"正在解压整合包（{done}/{total} 个文件）…"})
                        try:
                            self.store.save(self.state)
                        except Exception:
                            pass
                    self.path = await asyncio.to_thread(
                        extract_cached, self.path, _cache_root, _on_unpack)
                    self.state.progress.append({"status": "done", "key": "解压整合包",
                                                "source": self.path.name, "translated": "解压完成"})
                    self.store.save(self.state)
            # 修复：解压目录被单一顶层目录包裹（zip 里 xxxx/主文件夹 嵌套结构）→ 下钻到
            # 项目根。否则 detect_input_type/后续 mods 扫描/产物路径全基于包裹层父目录，
            # 嵌套整合包/地图/光影识别失败或产物结构错（用户实测）。非包裹目录幂等返回。
            self.path = unwrap_bare_wrapper(self.path)
            self.kind = detect_input_type(self.path)
            if self.kind == "map":
                # 委托地图流程（源语言/版本由 maps_flow 处理），返回
                await run_map_translation(
                    self.task_id,
                    MapTranslateRequest(path=str(self.path),
                                        source_lang=self.req.source_lang or "en_us",
                                        target_lang=self.req.target_lang),
                    self.cfg, self.store, self.work_dir, self.outputs_dir)
                return
            if self.kind == "shader":
                # 光影包汉化（第四模式）：扫描 shaders/lang/en_US.lang → 翻译 → 产出汉化包
                await self._stage_shader()
                return
            if self.kind == "unknown":
                self.state.status = "failed"
                self.state.progress.append({"status": "error",
                                            "error": "无法识别输入类型（支持整合包目录/压缩包、mod jar、地图、光影包）"})
                self.store.save(self.state)
                return
            if self.kind == "modjar" and not self.path.is_file():
                self.state.status = "failed"
                self.state.progress.append({"status": "error", "error": "mod jar 输入应为有效的 .jar 文件"})
                self.store.save(self.state)
                return

            # 聚 jar 列表前给反馈：解压后到扫描完成之间（rglob 遍历 573MB 解压目录可能几十秒）
            # 无任何 progress，右栏就停在「解压完成」干瞪眼（用户反馈）——先推「正在扫描」让流程有动静
            # v1.4.0 修复（用户「无论源文件什么类型都显示扫描整合包」）：文案按 kind 区分——
            # modjar/map/shader 不是整合包，显示各自类型的扫描文案（原来统一「整合包」误导）
            _kind_scan_text = {
                "modpack": "正在扫描整合包文件…",
                "modjar": "正在扫描 mod 文件…",
                "map": "正在扫描地图存档…",
                "shader": "正在扫描光影包…",
            }.get(self.kind, "正在扫描文件…")
            await self._smart_status(_kind_scan_text)
            # 聚 jar 列表（modpack: mods/**/*.jar；modjar: 该文件本身）
            # 聚 jar 列表（modpack: mods/**/*.jar；modjar: 该文件本身）。
            # 修复：rglob 遍历 573MB 解压目录可能几十秒，同步跑会阻塞事件循环（任务状态超时）→ to_thread
            if self.kind == "modpack":
                self.jars = sorted(await asyncio.to_thread(
                    lambda: (self.path / "mods").rglob("*.jar")))
            else:
                self.jars = [self.path]
            if not self.jars:
                self.state.status = "failed"
                self.state.progress.append({"status": "error",
                                            "error": ("未在整合包中找到任何 mod jar（mods/ 下无 .jar）"
                                                      if self.kind == "modpack" else "未找到可翻译的 jar")})
                self.store.save(self.state)
                return

            # 整合包目录文本源（任务线/config/data/kubejs）：只读扫描，译文进汉化补丁包
            if self.kind == "modpack":
                try:
                    # 目录文本源扫描（config/kubejs/data 可能几百文件）也 to_thread，防阻塞事件循环
                    # target_lang 透传：整合包目录 json（kubejs/assets/lang 等）target_path
                    # 按目标语言替换（en_us → zh_cn），否则译文写回 en_us 游戏看不到
                    self.pack_sources = await asyncio.to_thread(
                        discover_pack_text_sources, self.path, self.req.target_lang)
                except Exception as e:
                    self.state.progress.append({"status": "warn",
                                                "error": f"扫描整合包目录文本源失败：{e}"})

            # 源语言自动检测（req.source_lang 为空时）；全汉化返回 None → 兜底 en_us + warn
            auto_lang = (await asyncio.to_thread(detect_source_lang, self.jars, self.req.target_lang)
                         if not self.req.source_lang else None)
            if auto_lang is None and not self.req.source_lang:
                # 可继续：缺口为 0 自然空包，由下游空词条/无可导出分支覆盖
                self.state.progress.append({"status": "warn",
                                            "error": "源语言自动检测：所有资源已是目标语言，无空缺可翻（以 en_us 兜底继续）"})
            self.source_lang = self.req.source_lang or auto_lang or "en_us"

            # 语言文件扫描 → jobs（key 级跳过已汉化，支持任意源语言）。
            # 逐 jar 扫描 + 实时进度：整批 scan_modpack 期间右栏会停在「正在读取 N 个 mod」不动，
            # 用户不知道在读什么/读到哪/读没读完（用户反馈）→ 每扫完一个 jar 更新
            # 「正在读取 i/N 个 mod」，进度实时可见；扫描完成再推汇总明确读取成功。
            if self.kind == "modpack":
                scans = []
                _total = len(self.jars)
                for _i, _jar in enumerate(self.jars, 1):
                    if self.state.cancelled:
                        self.state.status = "cancelled"
                        self.store.save(self.state)
                        self._aborted = True
                        return
                    await self._wait_if_paused()
                    try:
                        scans.extend(await asyncio.to_thread(
                            scan_jar, _jar, self.source_lang, self.req.target_lang))
                    except Exception:
                        continue   # 单 jar 损坏跳过，不中断整包（_scan_one_jar 内部已容错，双保险）
                    self.state.progress.append({"status": "translating", "count": 0,
                                                "note": f"正在读取 {_i}/{_total} 个 mod 的语言文件…"})
                    self.store.save(self.state)
            else:
                scans = await asyncio.to_thread(scan_jar, self.path, self.source_lang,
                                                self.req.target_lang)
            self.state_jobs = build_jobs(scans, self.req.target_lang)
            # v1.3.4（用户「total 随时涨 1312→1611」）：语言文件 total 应为**全部值条目**
            #（含已汉化/记忆命中的），不是缺口 len(state_jobs)——记忆/CFPA/skip 命中在
            # pipeline 也 bump done 但不在缺口里 → done 超缺口 total → 此前 total 跟随掩盖
            #（total 乱涨）。全部值条目做 total，done（含记忆/CFPA/skip/翻译）恒 ≤ total。
            self._lang_total = sum(len(s.source_entries) for s in scans)
            # 扫描完成汇总（用户诉求：明确知道读取成功与否）
            # v1.4.0：进度 key 按 kind 区分（「扫描整合包」对单 mod/地图/光影误导）
            _scan_key = {"modpack": "扫描整合包", "modjar": "扫描 mod",
                         "map": "扫描地图", "shader": "扫描光影"}.get(self.kind, "扫描")
            self.state.progress.append({"status": "done", "key": _scan_key,
                                        "source": f"{len(self.jars)} 个 mod",
                                        "translated": f"发现 {len(self.state_jobs)} 条待翻译"})
            self.store.save(self.state)

            # 引擎创建（on_usage 回挂必须在 create_engine 前定义，供赋给 engine）
            self.engine = create_engine(self.cfg)
            # 熔断恢复基准（v1.2.3+）：记录初始并发/批次，降档后回升封顶于此（永不超初始）
            if isinstance(self.engine, LLMClient):
                self._circuit_initial_c = getattr(self.engine, "concurrency", None)
                self._circuit_initial_b = getattr(self.engine, "batch_size", 20)
            else:
                self._circuit_initial_c = None
                self._circuit_initial_b = self._batch_size
            self._circuit_reduced = False
            self._circuit_healthy = 0
            if hasattr(self.engine, "on_usage"):
                self.engine.on_usage = self._on_usage
            if isinstance(self.engine, LLMClient) and self.glossary_prompt:
                self.engine.glossary_prompt = self.glossary_prompt
            if isinstance(self.engine, LLMClient) and not self.engine.api_key:
                # R1：keyring 空 key → 引擎主路径假成功，提前告警
                self.state.progress.append({"status": "warn",
                                            "error": "未配置 API Key，AI 翻译将失败，请在配置页填写"})
            self.engine_machine = isinstance(self.engine, MachineClient)
            # 整合包汉化走「i18n 汉化资源包 + AI 补缺口」形式（用户刚需）：**跳过硬编码
            # AI 判断**——避免烧 token（实测 14119 条候选烧 361 万 input）且不产出
            # 「修改版 mod jar」（改字节码 = 二次分发修改版 mod，存在分发纠纷）。
            # 硬编码判断：**所有模式（整合包/单 mod）都扫描 + AI 判断**（用户刚需：
            # 整合包硬编码也要翻译，用 **VP 补丁（Vault Patcher）形式生效**——vault-patcher.jar
            # + 映射模块运行时注入，不碰 mod jar、不产修改版 jar，无二次分发纠纷）。
            # 只有在线机翻（无 AI 判断能力）才跳过硬编码。

            # 输入名翻译（右栏标题区完成态显示中文名 + 原名淡化）；失败回退原名不中断
            self.state.progress.append({"status": "translating", "count": 0,
                                        "note": "正在翻译文件名…"})
            self.store.save(self.state)
            await self._translate_input_name()

            # 全文本覆盖：结构化 JSON / en_us 文本（lines）→ 写回 jar 副本。
            # 语言文件（lang）已由 scan+jobs 走 by_mod 资源包，这里只收非 lang 源。
            # 逐 jar 遍历 zip 条目（355 个 jar 可能几十秒）：每扫完一个推「正在扫描 i/N 个 mod 的文本源」
            for _i, jar in enumerate(self.jars, 1):
                try:
                    srcs = [s for s in await asyncio.to_thread(
                                discover_text_sources, jar, self.req.target_lang)
                            if s.kind != "lang"]
                    if srcs:
                        self.text_sources_by_jar[jar] = srcs
                except Exception as e:
                    # 损坏 jar 异常兜底跳过，不让一个坏文件中断整包流程
                    self.state.progress.append({"status": "warn",
                                                "error": f"扫描 {jar.name} 文本源失败：{e}"})
                self.state.progress.append({"status": "translating", "count": 0,
                                            "note": f"正在扫描 {_i}/{len(self.jars)} 个 mod 的文本源…"})
                self.store.save(self.state)

            # 硬编码候选扫描（仅非 machine 引擎；LLM 走 AI 判断，兜底引擎走全翻）。
            # 整合包也扫描硬编码——翻译走 VP 补丁（Vault Patcher）生效，不产修改版 jar。
            if self.engine_machine:
                self.state.progress.append({"status": "warn",
                                            "error": "在线机翻无法 AI 判断硬编码，已跳过硬编码翻译"})
            else:
                # 硬编码扫描最耗时（解压 jar + 逐个 class 解析字节码）：并行处理。
                # Semaphore(4) 限流：同时最多解压/解析 4 个 jar，避免几百个 jar 一起打爆
                # 磁盘（解压临时目录）和内存（class 加载）。每完成一个 jar 推
                # 「正在扫描硬编码 i/N（jar 名）」，进度连贯实时（用户诉求：数据流在动）。
                # 扫描并发数取用户设置（config.scan_concurrency，默认 4，设置页可调）。
                # 测试/兜底路径 cfg 可能为 None → (self.cfg or {}) 容错回默认 4
                _hard_sem = asyncio.Semaphore(
                    max(1, int((self.cfg or {}).get("scan_concurrency") or 4)))
                _hard_done = [0]

                async def _scan_hard_one(jar: Path) -> None:
                    async with _hard_sem:
                        try:
                            if jar.stat().st_size > _HARDCODE_MAX_BYTES:
                                self.state.progress.append({"status": "warn",
                                                            "error": (f"跳过超大 jar {jar.name} 的硬编码扫描"
                                                                      f"（>{_HARDCODE_MAX_BYTES // 1024 // 1024}MB）")})
                            else:
                                cands = await asyncio.to_thread(scan_hardcoded_candidates, jar)
                                if cands:
                                    self.hard_candidates_by_jar[jar] = cands
                        except Exception as e:
                            # 损坏 jar 异常兜底跳过，不让一个坏文件中断整包流程
                            self.state.progress.append({"status": "warn",
                                                        "error": f"扫描 {jar.name} 硬编码字符串失败：{e}"})
                    _hard_done[0] += 1
                    self.state.progress.append({"status": "translating", "count": 0,
                                                "note": f"正在扫描硬编码 {_hard_done[0]}/{len(self.jars)} 个 mod（{jar.name}）…"})
                    self.store.save(self.state)

                await asyncio.gather(*(_scan_hard_one(jar) for jar in self.jars))

            # 词库自动准备：检测输入 MC 版本 → 词库已下载且版本匹配直接用，否则自动下载。
            # 下载/就绪/失败都在进度明细可见（translating/done/warn），不占翻译 total。
            # 仅中文目标：CFPA 是中文社区词库，日文/其他语言不下载也不用（__init__ 已置空 cfpa）
            if self.cfpa_path and self.req.target_lang in ("zh_cn", "zh_tw"):
                self.state.progress.append({"status": "translating", "count": 0,
                                            "note": "正在检测整合包 MC 版本…"})
                self.store.save(self.state)
                # 懒加载已下载的词库索引（to_thread 防阻塞事件循环——几十 MB 索引同步解析
                # 会让 /api/task 读取超时）
                self.cfpa = await asyncio.to_thread(load_cfpa, self.cfpa_path)
                _mc_ver = await asyncio.to_thread(_detect_mc_version, self.kind, self.path, self.jars)
                if _mc_ver:
                    self.cfpa = await self._ensure_cfpa(_mc_ver)

            # 进度总量：逐阶段明细（语言文件 jobs / jar 内 json+lines / 整合包目录文本源 /
            # 硬编码候选）。用户最新需求：进度条显示「语言文件 + 硬编码」一共数量 + 当前阶段。
            # 硬编码阶段 done 按候选数推进（可见翻译或 exclude 判定都算「已处理」）；
            # build 阶段在产物组织前补入（total 依赖阶段 1-3 的实际产出）。
            self.state.stages = [
                # v1.4.2 修复（用户「total 虚标 61885，实际 36740」）：lang total 只算
                # 「切切实实需要翻译的」（待翻译缺口），不算已汉化/记忆命中/跳过的。
                # 原 _lang_total = sum(len(source_entries)) 包含了全部条目（含已汉化），虚标。
                # 预扫描跳过的条目只计 stage done，不计全局 done——否则 done > total。
                {"name": "lang", "total": len(self.state_jobs), "done": 0},
                {"name": "json", "total": sum(
                    len(s.entries) for srcs in self.text_sources_by_jar.values() for s in srcs), "done": 0},
                {"name": "pack", "total": sum(len(s.entries) for s in self.pack_sources), "done": 0},
            ]
            if not self.engine_machine:
                self.state.stages.append({"name": "hardcode", "total": sum(
                    len(c) for c in self.hard_candidates_by_jar.values()), "done": 0})
            # v1.3.7 重构（用户「4,895/4,588 超 total / 进度条随实际变化」）：total **一次性
            # 定死**——只含翻译条目（lang+json+pack+hardcode），build 产物单位剥离出全局
            # 进度（build 阶段只推进 stage 明细，不 bump 全局 done）。
            # 续联保护（仅启动时一次）：续联基准 done 可能 > 当前扫描 total（删 mod/旧进度
            # 虚高）——取较大者定死，**此后永不跟随 done**（删除原三处 max(done+failed) 修正，
            # 双计由账本 _done_keys 根治，total 不再被拉高掩盖）。
            _scan_total = sum(s["total"] for s in self.state.stages)
            self.state.total = max(_scan_total, self.state.done + self.state.failed)
            self.state.stage = "lang"
            # 若语言/文本源为空但有硬编码候选（LLM/兜底引擎），仍需继续流程
            if self.state.total == 0 and not (self.hard_candidates_by_jar and not self.engine_machine):
                # 空词条（且无硬编码待判断）：直接 done + warn，不导出空包
                self.state.status = "done"
                self.state.progress.append({"status": "warn",
                                            "error": "未发现可翻译的词条（语言文件缺口与文本源/硬编码都为空）"})
                self.store.save(self.state)
                return
            self.store.save(self.state)

            self.same_script = is_same_script(self.source_lang, self.req.target_lang)
            self._batch_size = getattr(self.engine, "batch_size", 20)

            # 各阶段依次执行（共享 self 状态，方法间零参数）。
            # 修复：每阶段后统一查取消（lang/json 阶段经 pipeline 软取消也置 _aborted），
            # 否则取消后流程继续进 build 把 status 覆盖成 done——用户取消无效且显示完成
            await self._stage_lang()
            if self.state.cancelled or self._aborted:
                return
            await self._stage_json()
            if self.state.cancelled or self._aborted:
                return
            if not self.engine_machine:
                await self._stage_hardcode()
            if self.state.cancelled or self._aborted:
                return    # 取消：直接收尾（finally 清理），不继续 build

            # v1.1.0：AI 语境归一化（替代机械 _consistency_normalize 硬统一）——只对
            # 「同原文 ≥2 个译文」的**专名形态**候选，AI 判定语境是否相同；同语境 → AI
            # 在语境中重翻统一（规范译名提示），不同语境/常用词（light/right）保留不动。
            if not self.state.cancelled:
                try:
                    await self._ai_contextual_normalize()
                except Exception:
                    pass    # 归一化失败不阻断流程（产物已可用，只是可能不统一）

            # build 阶段：产物单位（modjar 1 个汉化 jar；modpack 新结构 = 汉化资源包目录
            # + 补丁解压 + 整合包汉化.zip + 硬编码 jar，各 1 个单位）。
            # 预估可能偏大（VP 成功跳过硬编码 jar），任务收尾时按实际产出兜底修正 total。
            if self.kind == "modjar":
                build_total = 1
            else:
                # 整合包：汉化资源包(1) + 补丁(1) + 整合包汉化.zip(1，i18n mod 内置总产) + 硬编码(1 可选)
                build_total = (1 if self.by_mod else 0) \
                    + (1 if self.pack_translations else 0) \
                    + 1 \
                    + (1 if self.hard_mappings else 0)
            self.state.stages.append({"name": "build", "total": build_total, "done": 0})
            # v1.3.7：build 产物单位**不并入全局 total**（total 已在扫描后定死=翻译条目）——
            # build 阶段只推进 stage 明细（_bump_stage_only），全局 done 在翻译完成时已到 total。
            # 删除原 `total = max(sum(stages), done+failed)`：build 单位预估 + 续联基准会被
            # 双计 done 拉高 total（用户「total 随时涨」根因），账本 + 定死 total 根治。
            self._set_stage("build")

            # 产物组织 outputs/<task_id>/ 下（exe 旁 outputs，download 从这里读）
            self.out_dir = self.outputs_dir / self.task_id
            self.out_dir.mkdir(parents=True, exist_ok=True)
            await self._stage_build()

            if not self.exported:
                # 全部词条已汉化 / 全部翻译失败：done + warn，不导出空包
                self.state.status = "done"
                self.state.progress.append({"status": "warn",
                                            "error": "无可导出的翻译产物（词条均为已汉化或全部失败）"})
                if self.failures:
                    self.state.progress.append({"status": "warn", "key": f"未翻译 {len(self.failures)} 条",
                                                "untranslated": self.failures[:60]})
                self.store.save(self.state)
                return

            # v1.3.7：build 预估单位与实际产出兜底对账——**只修 build stage 明细 total**
            #（VP 失败等预估偏大时 build 明细能到顶），全局 total 已定死（=翻译条目），
            # **不再跟随 done**（删除原 total = done+failed 修正——那是「total 随时涨」根因）。
            for s in self.state.stages:
                if s["name"] == "build":
                    s["total"] = s["done"]
                    break
            # v1.3.9 修复（用户「总进度 80% 与阶段 7,391/8,208 不一致」）：
            # **任务完成时把各阶段 done 补足到 total**——跳过/已汉化/CFPA 命中/记忆命中等
            # 「不真正调 AI 但已处理」的条目，用户要求算进总进度（总进度必须 100%），
            # 只有**覆盖率**才用 `done - skipped` 排除跳过（report.py 已按 _skipped_n 扣）。
            # 补足只调**当前 stage 的 done**（各阶段已完成，stage 已切到 build），
            # 逐阶段补：lang/json/pack/hardcode 的 done 对齐各自 total，全局 done 同步。
            # v1.3.9 补足（修正）：把「跳过/已处理但未计 done」的条目补进各阶段 done——
            # 用户要求总进度到 100%（跳过只影响覆盖率，不影响总进度）。
            # **但失败条目（failed）不补**：`done + failed` 恒 ≤ total（v1.3.7 不变量）。
            # 各阶段按 total 补满，但全局 done 封顶 `total - failed`（保证 done+failed=total）；
            # 无失败时 done = total（100%），有失败时显示真实完成度（诚实）。
            for s in self.state.stages:
                if s["name"] != "build" and s["done"] < s["total"]:
                    s["done"] = s["total"]
            _new_total_done = sum(s["done"] for s in self.state.stages)
            self.state.done = min(max(_new_total_done, self.state.done),
                                  self.state.total - self.state.failed)

            self.state.status = "done"
            # modjar 无资源包 zip：pack 字段仅 modpack 且语言文件非空时指向成品 zip，否则 None
            self.state.progress.append({"status": "done", "file": str(self.out_dir),
                                        "pack": (str(self.out_dir / "整合包汉化.zip")
                                                 if self.kind == "modpack" and self.by_mod else None),
                                        "hardcoded": self.hard_count})
            # 未翻译明细最后追加 → 前端「最新在前」把它顶到最上方（置顶用户要看的失败条目）
            if self.failures:
                self.state.progress.append({"status": "warn", "key": f"未翻译 {len(self.failures)} 条",
                                            "untranslated": self.failures[:60]})
            self.store.save(self.state)
        except asyncio.CancelledError:
            # CancelledError 继承 BaseException，逃过 except Exception 会状态卡死（F2）
            self.state.status = "cancelled"
            self.store.save(self.state)
            raise
        except Exception as e:
            self.state.status = "failed"
            self.state.progress.append({"status": "error", "error": sanitize_error(str(e))})
            self.store.save(self.state)
        finally:
            # 注销运行中注册表（吞吐热更新不再作用于已结束任务）
            with _flows_lock:
                RUNNING_FLOWS.pop(self.task_id, None)
            # 释放引擎 HTTP 连接池（P1-8）：任务结束关闭 httpx，防连接堆积
            eng = getattr(self, "engine", None)
            if eng is not None and hasattr(eng, "aclose"):
                try:
                    await eng.aclose()
                except Exception:
                    pass
            # 任务终态（done/failed/cancelled）后清理任务级中间产物（temp），产物保留（C）
            if self.state.status in ("done", "failed", "cancelled"):
                # P1-4：任务结束清理暂停事件（防 _pause_events 表随任务累积泄漏）
                self.store.discard_pause_event(self.task_id)
                # 生成翻译报告（通用所有模式；地图由 maps/flow 单独生成——失败/产物数据在 flow 侧）。
                # 修复（recheck）：报告在 cleanup **之前**生成——cleanup 删 build 组装区
                # （hardcoded jar 所在），之前先 cleanup 会让报告 products 引用已删文件 size=0。
                if getattr(self, "kind", "") != "map":
                    try:
                        self._save_report()
                    except Exception:
                        pass
                cleanup_task_work(self.work_dir, self.task_id)
                # 项目进度保存（断点续联提示用；崩溃时 finally 也会执行，进度不丢）。
                # _save_progress 对 done 状态把 progress 写满 total → 断点续联列表不再显示
                # 已完成项目；任务快照 done/total 保持真实（不虚增，维持 done+failed<=total
                # 不变量），进度条 100% 由前端「status==done → 100%」呈现。
                try:
                    self._save_progress()
                except Exception:
                    pass


async def run_auto_translation(task_id: str, req: AutoRequest, cfg: AppConfig,
                               store: TaskStore, work_dir: Path, outputs_dir: Path,
                               cfpa_path: Path | None = None) -> None:
    """统一全自动翻译入口：编排委托 AutoFlow 类（P2-1 流程化拆分）。

    work_dir 为中间产物区（temp，任务终态后清理任务级子目录）；
    outputs_dir 为产物区（exe 旁 outputs/，资源包/汉化 jar 落这里）。
    """
    flow = AutoFlow(task_id, req, cfg, store, work_dir, outputs_dir, cfpa_path)
    await flow.run()
