import hashlib
import re
import shutil
import threading
import zipfile
from pathlib import Path
from typing import Callable

def is_archive(path: Path) -> bool:
    """是否整合包压缩包（zip / mrpack）。"""
    return path.suffix.lower() in (".zip", ".mrpack")

# 解压缓存并发锁：detect/autoTranslate 可能同时解压同一指纹目录，防互相清理
_EXTRACT_LOCK = threading.Lock()

# 安全解压上限：总未压缩体积 2GB / 条目数 2 万（正常整合包远低于此，超限判恶意）
_MAX_TOTAL = 2 * 1024 ** 3
_MAX_ENTRIES = 20000

# Windows 保留设备名（解压条目含这些 → 拒绝；NTFS 上 con/nul 等会触发异常/拒绝服务）
_WIN_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}

_WINDOWS_BAD_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_windows_entry(name: str) -> str:
    """zip 条目名 Windows 文件系统安全化（与 hardcode._sanitize_entry 同款，hardcode
    已修、archive 漏网——修复 recheck）。NeoForge data 标签等脏条目名**尾随空格**
    （`worldgen /biome`）Windows 不允许以空格结尾，mkdir/open 抛 WinError 中断整个
    整合包解压；含 `<>:"|?*` 与控制字符同理。逐段清理：去首尾空白/尾随点、非法字符
    替换 `_`。清理后空串（全非法）返回空，调用方跳过该条目。
    """
    parts = []
    for seg in name.split("/"):
        seg = _WINDOWS_BAD_CHARS_RE.sub("_", seg.strip().rstrip(". "))
        parts.append(seg)
    return "/".join(parts)


def safe_extract(zf: zipfile.ZipFile, dest: Path,
                 on_progress: Callable[[int, int], None] | None = None) -> None:
    """安全解压：zip-slip 防护 + 解压体积/条目数上限。

    zipfile.extractall 对 ../ 的剥离行为依赖 Python 版本（低版本可能越界写出），
    这里显式校验 namelist 拒绝路径穿越/绝对路径/符号链接，并对 zip 炸弹设上限。
    任一违规抛 ValueError（调用方按「无法解压」处理）。
    on_progress(done, total)：逐文件解压，每 50 个回调一次（或最后一个）——让前端
    「正在解压整合包（i/N 个文件）」实时跳动（用户诉求：每个阶段数据流在动）。
    """
    dest_resolved = dest.resolve()
    infos = zf.infolist()
    if len(infos) > _MAX_ENTRIES:
        raise ValueError(f"zip 条目数超限（{len(infos)}），疑似恶意压缩包")
    total = 0
    for info in infos:
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or (len(name) >= 2 and name[1] == ":"):
            raise ValueError(f"zip 条目含绝对路径：{info.filename}")
        if any(seg == ".." for seg in name.split("/")):
            raise ValueError(f"zip 条目含路径穿越：{info.filename}")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:  # S_IFLNK 符号链接
            raise ValueError(f"zip 条目为符号链接：{info.filename}")
        # 修复（recheck）：Windows NTFS ADS（foo.txt:evil）与保留设备名（con/nul/com1 等，
        # 含 con.txt 形态）在 zf.extract 时触发异常/拒绝服务——预检拒绝
        seg = name.split("/")[-1]
        if ":" in seg:
            raise ValueError(f"zip 条目含 NTFS 数据流：{info.filename}")
        if seg.lower().split(".")[0] in _WIN_RESERVED:
            raise ValueError(f"zip 条目为 Windows 保留设备名：{info.filename}")
        total += info.file_size
        if total > _MAX_TOTAL:
            raise ValueError("解压总大小超限，疑似 zip 炸弹")
    n = len(infos)
    _written = 0
    for i, info in enumerate(infos):
        # 修复（recheck）：流式解压并累计**实际写出字节**——中央目录声明的 file_size 可伪造
        #（zip 炸弹防护：deflate 极限压缩比 ~1000:1，声明 2MB 可解压出近 2GB）。zf.extract
        # 直接写盘无法计数，改用 zf.open + 分块写目标文件。
        if info.is_dir():
            continue
        # 修复（recheck）：Windows 非法文件名清理（NeoForge data 标签等脏条目名尾随
        # 空格/非法字符，Windows mkdir/open 抛 WinError 中断整个整合包解压）
        clean = _sanitize_windows_entry(info.filename.replace("\\", "/"))
        if not clean:
            continue   # 全非法条目跳过
        target = dest / clean
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    _written += len(chunk)
                    if _written > _MAX_TOTAL:
                        raise ValueError("解压实际体积超限，疑似 zip 炸弹")
                    out.write(chunk)
        except OSError as e:
            # 修复（recheck）：恶意/脏 zip 可构造「目录条目 a/ + 同名文件条目 a」——
            # open(target) 抛 IsADirectoryError 中断整个解压；逐条跳过继续（zip 炸弹
            # ValueError 不在此捕获，仍需冒泡）
            continue
        if on_progress and (i % 50 == 0 or i == n - 1):
            on_progress(i + 1, n)


def extract_modpack(archive: Path, dest: Path,
                    on_progress: Callable[[int, int], None] | None = None) -> Path:
    """解压整合包压缩包到 dest（自动建目录），返回解压根目录。
    zip 与 mrpack 同为 zip 容器；mrpack 根含 mods/、overrides/，解压后按普通目录扫。"""
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as zf:
            safe_extract(zf, dest, on_progress=on_progress)
    except (zipfile.BadZipFile, ValueError) as e:
        # 损坏/非法 zip 容器：抛带路径的明确异常，便于上层定位坏文件
        raise ValueError(f"无法解压整合包 {archive}: {e}") from e
    return dest


def archive_fingerprint(path: Path) -> str:
    """整合包 zip 指纹：**zip 内容指纹**（中央目录条目名 + 大小 + 压缩大小）hash。

    修复：原「路径 + 大小 + mtime」——同一份整合包换路径/复制（mtime 变）就重新解压
    （用户实测「多解压一遍」）。改为读 zip 中央目录（不读全部内容，快），
    同一份整合包内容相同 → 指纹恒定 → 断点续联不重复解压；内容改动才重新解压。
    """
    try:
        with zipfile.ZipFile(path) as zf:
            # 中央目录：条目名 + 未压缩/压缩大小（内容相关，不读解压数据）
            central = "|".join(
                f"{i.filename}|{i.file_size}|{i.compress_size}" for i in zf.infolist())
        raw = central
    except Exception:
        # 损坏/非 zip：回退路径+大小+mtime（罕见）
        try:
            st = path.stat()
            raw = f"{path.resolve()}|{st.st_size}|{st.st_mtime_ns}"
        except OSError:
            raw = str(path)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def extract_cached(archive: Path, cache_root: Path,
                   on_progress: Callable[[int, int], None] | None = None) -> Path:
    """指纹缓存解压：已完整解压过（.done 标记）直接复用 → 相同整合包不重复解压
    （断点重连，用户诉求）；首次/文件改动时解压并写 .done。

    并发保护：模块锁防 detect/autoTranslate 同时解压同一指纹目录互相清理；
    解压失败清掉半截目录。新解压后清理旧缓存（只留最近 keep 个，防占空间）。
    """
    cache_root.mkdir(parents=True, exist_ok=True)
    dest = cache_root / archive_fingerprint(archive)
    with _EXTRACT_LOCK:
        if (dest / ".done").exists():
            return dest   # 复用缓存：断点重连，不重新解压
        shutil.rmtree(dest, ignore_errors=True)   # 半截/损坏缓存清掉重解
        dest.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(archive) as zf:
                safe_extract(zf, dest, on_progress=on_progress)
            (dest / ".done").write_text("ok", encoding="utf-8")
        except Exception:
            shutil.rmtree(dest, ignore_errors=True)
            raise
    clean_extracted_cache(cache_root)
    return dest


def clean_extracted_cache(cache_root: Path, keep: int = 3) -> None:
    """解压缓存只保留最近 keep 个（按 mtime），旧缓存清理防占空间（用户诉求）。"""
    try:
        dirs = sorted(
            (d for d in cache_root.iterdir()
             if d.is_dir() and (d / ".done").exists()),
            key=lambda d: d.stat().st_mtime, reverse=True)
        for d in dirs[keep:]:
            shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def dir_fingerprint(path: Path) -> str:
    """目录指纹：路径 + 文件数 + 总大小 + 最新 mtime 的 hash（只 stat 不读内容，快）。

    相同目录（世界存档/光影包等）自动识别并复用副本（断点重连，用户诉求）；
    内容改动后指纹变化 → 重新复制。仅 stat 元数据，比整档复制快得多。
    """
    total = 0
    count = 0
    newest = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                st = f.stat()
                total += st.st_size
                count += 1
                # 修复：聚合所有文件纳秒 mtime——秒级只取「最新文件」会在非最新文件
                # 内容变化（大小不变）时指纹不变 → 断点重连复用旧副本漏掉新改动
                newest = max(newest, st.st_mtime_ns)
    except OSError:
        pass
    return hashlib.md5(
        f"{path.resolve()}|{count}|{total}|{newest}".encode("utf-8")).hexdigest()[:12]
