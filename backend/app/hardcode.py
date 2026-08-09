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

import io
import re
import shutil
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath

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


def scan_hardcoded_candidates(jar: Path) -> list[dict]:
    """扫描硬编码候选：返回 [{"text": str, "occurrences": int}]，按出现频率降序。

    与 scan_hardcoded_strings 同一提取逻辑（解压 → 遍历 *.class → jawa 提取
    String 字面量 → is_hardcode_translatable 过滤），但用 Counter 保留频率，
    供前端候选选择列表按频率排序展示。
    """
    work = jar.parent / f".{jar.stem}_hw"
    counts: Counter[str] = Counter()
    try:
        _extract_jar(jar, work)
        loader = _class_loader(work)
        for p in sorted(work.rglob("*.class")):
            name = _class_name(p, work)
            try:
                klass = loader[name]
            except Exception:
                continue  # 单个 class 损坏/不可加载：跳过，不拖垮整包扫描
            for c in klass.constants:
                if isinstance(c, String):
                    t = c.string.value
                    if is_hardcode_translatable(t):
                        counts[t] += 1
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return [{"text": t, "occurrences": n} for t, n in counts.most_common()]


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
