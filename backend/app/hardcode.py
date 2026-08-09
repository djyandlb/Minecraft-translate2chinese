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
from pathlib import Path, PurePosixPath

from jawa.classloader import ClassLoader
from jawa.constants import String

from app.translate.common import should_translate

# 重新打包时跳过的 JAR 签名文件：改过字节码后签名必然失效，留着会让 JVM 拒载
_SIGNATURE_SUFFIXES = (".SF", ".RSA", ".DSA", ".EC")

# 类路径片段（com.example.Mod 的每个 "." 分隔段）
_CLASS_PATH_PART_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


def is_hardcode_translatable(text: str) -> bool:
    """判断一段字节码字符串字面量是否值得硬编码汉化。

    先复用 should_translate 过滤技术串，再追加字节码场景规则：
      - 长度裁剪到 [2, 100]
      - 纯大写缩写（OK/FPS）跳过
      - modid:item 命名空间串跳过
      - 类路径（com.example.Mod）跳过
    """
    t = text.strip()
    if not should_translate(t):
        return False
    if len(t) < 2 or len(t) > 100:
        return False
    # 纯大写缩写（OK/FPS）：全大写且至少含一个 ASCII 字母
    if t.isupper() and any(ch.isascii() and ch.isalpha() for ch in t):
        return False
    # modid:item（冒号且无空格）
    if ":" in t and " " not in t:
        return False
    # 类路径（每段都是合法 Java 标识符）
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


def scan_hardcoded_strings(jar: Path) -> list[str]:
    """扫描 jar 内硬编码的可翻译字符串。

    解压 jar → 遍历 *.class → jawa 提取 String 字面量 → 去重 →
    is_hardcode_translatable 过滤 → 排序返回。
    """
    work = jar.parent / f".{jar.stem}_hw"
    result: set[str] = set()
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
                        result.add(t)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return sorted(result)


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
                for c in klass.constants:
                    if isinstance(c, String) and c.string.value in mapping:
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
                        # 重读校验：能解析且 String 数不变才认成功
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
