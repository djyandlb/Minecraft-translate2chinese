# -*- coding: utf-8 -*-
"""CFPA 社区人工翻译词库接入（简体中文资源包）。

从 CFPA「简体中文资源包」（社区人工翻译，几千 mod 的 lang 翻译）下载对应 MC 版本
的 zip，建立 `{modid\x00key: 译文}` 索引。翻译时语言文件阶段**优先查词库命中**
（零成本高质量人工翻译），未命中的才走 AI。数据源是人工校对结果，比 AI 更贴合
社区公认译名（如「附魔金苹果」而非 AI 的直译）。

数据源：TranslationPackMirror 镜像（每日更新，对应版本 zip）。
CFPA 资源包只含 zh_cn 语言文件（无 en_us 原文），故索引为「key → 译文」，
只服务语言文件阶段（modid+key 精确匹配）；json/lines/硬编码无原文对照，仍走 AI。
"""
import asyncio
import io
import json
import re
import zipfile
from pathlib import Path

import httpx

# 支持的 CFPA 版本 zip（TranslationPackMirror files 目录命名）。
# 命名形如 Minecraft-Mod-Language-Modpack-1-20.zip（搜索结果确认）。
# 1.10.2 已移除：TranslationPackMirror 未收录（CFPA 停更太老版本），内置也无此包。
_SUPPORTED_ZIPS = [
    ("1.12.2", "Minecraft-Mod-Language-Modpack-1-12-2.zip"),
    ("1.16", "Minecraft-Mod-Language-Modpack-1-16.zip"),
    ("1.18", "Minecraft-Mod-Language-Modpack-1-18.zip"),
    ("1.19", "Minecraft-Mod-Language-Modpack-1-19.zip"),
    ("1.20", "Minecraft-Mod-Language-Modpack-1-20.zip"),
    ("1.21", "Minecraft-Mod-Language-Modpack-1-21.zip"),
]

# 内置 CFPA 汉化资源包目录（用户刚需：汉化包内置应用，离线可用，整合包优先走
# 现成人工翻译 + 资源包形式，不依赖在线下载）。源码：backend/app/data/cfpa/；
# PyInstaller frozen：_internal/app/data/cfpa/（spec 的 datas 打进）。
import sys
from pathlib import Path


def bundled_dir() -> Path:
    """内置 CFPA 汉化包目录（frozen 走 _MEIPASS，源码走包内 data）。"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "app" / "data" / "cfpa"


def update_dir() -> Path:
    """应用目录 data/（可写、持久——清 temp 缓存不删）：「检查更新」下载的内置资源更新版
    （i18n 汉化 mod / vp 硬编码 mod / cfpa 词库）落这里，**优先于**只读内置 _MEIPASS。

    **与内置分离是有意设计**：内置（源码 app/data、frozen _MEIPASS/app/data）是只读基线，
    更新版写独立目录（frozen → exe 同目录/data、源码 → backend/data），不污染内置、可持久。
    调用方（bundled_i18n_jar/bundled_vp_jar）优先读这里，找不到才回退内置。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "data"
    return Path(__file__).resolve().parent.parent / "data"


def list_bundled_versions() -> list[dict]:
    """列出应用内置的 CFPA 汉化包版本（{version, name, size_mb}），前端显示/诊断。"""
    d = bundled_dir()
    out: list[dict] = []
    if d.is_dir():
        for name in sorted(d.glob("*.zip")):
            out.append({"name": name.name,
                        "size_mb": round(name.stat().st_size / 1048576, 1)})
    return out


def bundled_i18n_jar() -> Path | None:
    """I18nUpdateMod（i18n 汉化下载器 mod，~49KB 全版本兼容）jar 路径——
    整合包产物 mods/ 目录用（用户刚需：i18n 是 mod 应放 mods 文件夹，进游戏自动
    下载 CFPA 全量汉化资源包）。**优先应用目录更新版**（检查更新下载到 update_dir()/i18n/，
    持久不清理），否则内置（源码 app/data/i18n/；frozen _MEIPASS/app/data/i18n/）。"""
    up = update_dir() / "i18n"
    if up.is_dir():
        for p in sorted(up.glob("*.jar")):
            return p
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    d = base / "app" / "data" / "i18n"
    if d.is_dir():
        for p in sorted(d.glob("*.jar")):
            return p
    return None


def load_bundled_cfpa(mc_version: str) -> dict | None:
    """从内置资源选匹配版本的 CFPA 汉化包 → 建词库索引。离线可用、不依赖网络。

    版本匹配同 match_zip_name（取 ≤ 输入版本的最大支持版本）；内置缺失返回 None
    （调用方回退在线下载/仅 AI）。
    """
    zip_name = match_zip_name(mc_version)
    if not zip_name:
        return None
    p = bundled_dir() / zip_name
    if not p.exists():
        return None
    try:
        glossary = build_index(p.read_bytes())
    except (zipfile.BadZipFile, OSError):
        return None
    glossary["mc_version"] = zip_name
    glossary["bundled"] = True
    glossary["size_mb"] = round(p.stat().st_size / 1048576, 1)
    return glossary

# 下载源（依次尝试，前一个失败换下一个）。2026-08-10 实测：
#  gitcode 返回 200 但是 HTML 错误页（非 zip）、staticaly 连不上、ghproxy 断连——全挂。
#  可用源：jsdelivr CDN（最快）、ghfast 代理、raw.githubusercontent 直连（国内可能慢）。
_DOWNLOAD_URLS = [
    "https://cdn.jsdelivr.net/gh/zkitefly/TranslationPackMirror@main/files/{name}",
    "https://ghfast.top/https://raw.githubusercontent.com/zkitefly/TranslationPackMirror/main/files/{name}",
    "https://raw.githubusercontent.com/zkitefly/TranslationPackMirror/main/files/{name}",
    "https://gitcode.net/chearlai/translationpackmirror/-/raw/main/files/{name}",
]

_VERSION_RE = re.compile(r"(\d+)\.(\d+)")


def _parse_ver(v: str) -> tuple[int, int]:
    m = _VERSION_RE.search(v or "")
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2)))


def match_zip_name(mc_version: str) -> str | None:
    """按 MC 版本选 CFPA zip 名：取 ≤ 输入版本的最大支持版本（1.20.1 → 1-20）。

    无匹配（版本过老，如 1.9）返回 None。
    """
    target = _parse_ver(mc_version)
    best_name: str | None = None
    best_ver: tuple[int, int] | None = None
    for ver, name in _SUPPORTED_ZIPS:
        v = _parse_ver(ver)
        if v <= target and (best_ver is None or v > best_ver):
            best_name = name
            best_ver = v
    return best_name


def build_index(zip_bytes: bytes) -> dict:
    """从 CFPA 资源包 zip 建立 {modid\x00key: 译文} 索引。

    遍历 assets/<modid>/lang/zh_cn.json；json 损坏/非法编码跳过该文件不中断。
    返回 {"by_key": {...}, "count": N}。
    """
    by_key: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            if not name.endswith("zh_cn.json"):
                continue
            parts = name.split("/")
            if len(parts) < 3 or parts[0] != "assets":
                continue
            modid = parts[1]
            try:
                data = json.loads(zf.read(name).decode("utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            for k, v in data.items():
                if isinstance(v, str) and v.strip():
                    by_key[f"{modid}\x00{k}"] = v
    return {"by_key": by_key, "count": len(by_key)}


def load_cfpa(path: Path) -> dict:
    """加载词库索引；文件不存在/损坏返回空结构（不影响流程）。"""
    if not path.exists():
        return {"by_key": {}, "count": 0, "mc_version": "", "size_mb": 0.0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("by_key", {})
        data.setdefault("count", len(data["by_key"]))
        return data
    except Exception:
        return {"by_key": {}, "count": 0, "mc_version": "", "size_mb": 0.0}


def save_cfpa(glossary: dict, path: Path) -> None:
    """落盘词库索引。"""
    path.write_text(json.dumps(glossary, ensure_ascii=False), encoding="utf-8")


# 全局下载进度（设置页轮询 /api/cfpa/status 展示）：词库 zip 可能数十 MB，
# 一次性请求期间前端只有「下载中…」字样，用户看不到进度干等（M2）。
# on_progress 回调写这里；download 完成/失败后 active 置 False（避免残留假进度）。
_dl_progress: dict = {"active": False, "phase": "", "downloaded": 0, "total": 0, "pct": 0}


def get_cfpa_progress() -> dict:
    """当前词库下载进度（下载完成/失败后 active=False，前端不显示进度条）。"""
    return dict(_dl_progress)


async def download_cfpa(mc_version: str, target_path: Path,
                        client: httpx.AsyncClient | None = None,
                        on_progress=None) -> dict | None:
    """下载匹配 MC 版本的 CFPA 词库 zip → 建索引 → 存 target_path。

    下载失败（无网络/404/超时）返回 None（调用方提示可重试）。client 可注入（测试 mock）。

    on_progress: 可选回调，流式下载逐块上报 {"downloaded", "total", "pct"}——
    设置页传（写全局进度供 /status 轮询），任务内 _ensure_cfpa 不传（走任务进度明细）。
    """
    zip_name = match_zip_name(mc_version)
    if not zip_name:
        return None
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=120)   # 词库 zip 可能数 MB~数十 MB，下载慢需放宽
    if on_progress is not None:
        _dl_progress.update(active=True, phase="connecting", downloaded=0, total=0, pct=0)
    try:
        content: bytes | None = None
        for url_tpl in _DOWNLOAD_URLS:
            try:
                if on_progress is not None:
                    _dl_progress["phase"] = "downloading"
                total = 0
                buf = bytearray()
                # 流式下载（替代一次性 resp.content）：逐块写 + 上报进度，大文件不再干等。
                # follow_redirects=True：部分源（gitcode 等）会 302 跳转真实文件，默认不跟随会直接失败
                async with client.stream("GET", url_tpl.format(name=zip_name),
                                         follow_redirects=True) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("content-length") or 0)
                    async for chunk in resp.aiter_bytes():
                        buf += chunk
                        if on_progress is not None:
                            _dl_progress["downloaded"] = len(buf)
                            _dl_progress["total"] = total
                            _dl_progress["pct"] = (round(len(buf) / total * 100)
                                                   if total else 0)
                            on_progress(dict(_dl_progress))
                if buf:
                    content = bytes(buf)
                    break
            except Exception:
                continue
        if not content:
            return None
        try:
            # 词库 zip 可能几十 MB、几万条 json：同步解析/写盘会阻塞事件循环
            # （→ /api/task 读取超时 60s、下载完不继续、SSE 卡死），丢线程池不阻塞。
            glossary = await asyncio.to_thread(build_index, content)
        except (zipfile.BadZipFile, OSError):
            # 修复：镜像返回 200 但内容非 zip（HTML 错误页）→ BadZipFile 冒泡会让
            # /api/cfpa/download 500；这里返回 None 走「下载失败」统一提示
            return None
        glossary["mc_version"] = zip_name
        glossary["size_mb"] = round(len(content) / 1048576, 1)
        await asyncio.to_thread(save_cfpa, glossary, target_path)
        return glossary
    finally:
        if on_progress is not None:
            _dl_progress["active"] = False
        if own:
            await client.aclose()
