# -*- coding: utf-8 -*-
"""Vault Patcher（VP）集成：整合包硬编码汉化的运行时替换方案。

整合包硬编码汉化：生成 VP 动态替换模块 JSON（data_dynamic 全匹配），并从 Modrinth
自动下载对应 loader + MC 版本的 VP jar。下载失败/联网不可用由 auto_flow 回退到
hardcoded 汉化 jar（替换 mod）。原整合包只读，本模块不写原包。

VP 模块文件放 vaultpatcher/modules/（新版本路径，VP 会自动从旧
config/vaultpatcher_asm 迁移）。格式按官方文档字段约定。
"""

import json
import logging
import re
import sys
import zipfile
from collections import Counter
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Modrinth 上 Vault Patcher 项目版本 API
_MODRINTH_VP_URL = "https://api.modrinth.com/v2/project/vault-patcher/version"

# MC 版本号（1.20 / 1.20.1；修复 recheck：支持 26.x 等新主版本——之前只匹配 1.x，
# 对 '26.1.1' 会在 index=3 错切出 '1.1' 导致版本推断错误）
_MC_VERSION_RE = re.compile(r"(\d+\.\d{1,2}(?:\.\d{1,2})?)")


def _version_matches(mc_version: str, game_versions: list) -> bool:
    """版本匹配（修复：`[1.20,1.21)` 提取出 1.20，Modrinth 的 game_versions 通常只有
    完整版本 1.20.1/1.20.2，精确字符串匹配必失败 → VP 方案静默回退。改为主版本前缀
    双向放宽匹配）。"""
    if mc_version in game_versions:
        return True
    for gv in game_versions:
        if gv.startswith(mc_version + ".") or mc_version.startswith(gv + "."):
            return True
    return False


def _extract_mc_version(spec: str | None) -> str | None:
    """从版本约束（>=1.20.1 / [1.20,1.21) / 1.20.1）提取首个 MC 版本号。"""
    m = _MC_VERSION_RE.search(spec or "")
    return m.group(1) if m else None


def _toml_mc_spec(zf: zipfile.ZipFile, toml_path: str) -> str | None:
    """从 mods.toml / neoforge.mods.toml 找 dependencies 里 modId=minecraft 的 versionRange。

    声明形如 [[dependencies.<modid>]]，tomllib 解析后落在 data["dependencies"][<modid>]
    = [...]（嵌套 dict）；兼容扁平 dotted key（dependencies.<modid>）。
    """
    import tomllib
    data = tomllib.loads(zf.read(toml_path).decode("utf-8"))
    deps_root = data.get("dependencies")
    if isinstance(deps_root, dict):
        for dep_list in deps_root.values():
            if not isinstance(dep_list, list):
                continue
            for dep in dep_list:
                if isinstance(dep, dict) and dep.get("modId") == "minecraft":
                    return dep.get("versionRange")
    for deps_key, deps in data.items():
        if deps_key.startswith("dependencies.") and isinstance(deps, list):
            for dep in deps:
                if isinstance(dep, dict) and dep.get("modId") == "minecraft":
                    return dep.get("versionRange")
    return None


def infer_modpack_runtime(mods_dir: Path) -> tuple[str, str]:
    """从 mods/ 元数据推断整合包 loader + MC 版本（**众数投票**，不取第一个）。

    返回 (loader, mc_version)；loader ∈ {"fabric","forge","neoforge"}，
    无法推断时均为空串。遍历 mods/**/*.jar 读 fabric.mod.json /
    neoforge.mods.toml / mods.toml，统计每个 jar 声明的 loader/版本出现次数。

    v1.5.0 修复（用户实测「1.21.4 整合包材质包版本不兼容」）：原实现取**第一个**
    命中的 jar——mods 目录遍历顺序不稳定，碰巧命中声明「依赖范围下限」
    （depends.minecraft=">=1.21"）的 jar → 判成 1.21 → pack_format 写成 34 →
    1.21.4 游戏拒载。改众数：多数 mod 声明的主流版本胜出（1.21.4 整合包里
    58/110 个 jar 声明 1.21.4），平局取版本最大。单个损坏 jar 跳过不中断。
    """
    if not mods_dir.is_dir():
        return ("", "")
    loader_votes: Counter = Counter()
    ver_votes: Counter = Counter()
    for jar in sorted(mods_dir.rglob("*.jar")):
        try:
            with zipfile.ZipFile(jar) as zf:
                names = zf.namelist()
                if "fabric.mod.json" in names:
                    data = json.loads(zf.read("fabric.mod.json").decode("utf-8"))
                    depends = data.get("depends") or {}
                    v = _extract_mc_version(str(depends.get("minecraft", ""))) or ""
                    if v:
                        loader_votes["fabric"] += 1
                        ver_votes[v] += 1
                    continue
                for toml, loader in (("META-INF/neoforge.mods.toml", "neoforge"),
                                     ("META-INF/mods.toml", "forge")):
                    if toml in names:
                        v = _extract_mc_version(_toml_mc_spec(zf, toml)) or ""
                        if v:
                            loader_votes[loader] += 1
                            ver_votes[v] += 1
                        break
        except Exception:
            continue
    return (_top_vote(loader_votes), _top_vote(ver_votes))


def _top_vote(votes: Counter) -> str:
    """取出现次数最多的 key；平局取版本号最大（1.9 < 1.10 按数值比较，非字符串）。"""
    if not votes:
        return ""
    best = max(votes.values())
    cands = [k for k, c in votes.items() if c == best]
    if len(cands) == 1:
        return cands[0]
    try:
        return max(cands, key=lambda v: tuple(int(x) for x in v.split(".")))
    except ValueError:
        return max(cands)


def build_vp_module(vp_pairs: dict[str, str]) -> list[dict]:
    """生成 VP 动态替换模块 JSON（data_dynamic 全匹配）。

    JSON 数组：模块信息头（name/authors/mods/desc/data_dynamic/data_i18n）+
    转换规则对象（target_classes 留空 → 全局动态匹配）。
    全匹配：value 不以 @ 开头，仅整个字符串与 key 完全相同时替换。

    **修复：pairs 必须是键值对数组 [{key,value}]**——Vault Patcher 官方格式
    （DeepWiki：pairs 类型为键值对数组，支持长格式 key/value、短格式 k/v）。
    此前生成 dict {"原文":"译文"} 会被 VP 拒读/不加载 → 硬编码映射完全失效
    （用户质疑「产物适配 VP 没有」的根因）。"""
    pairs = [{"key": k, "value": v} for k, v in (vp_pairs or {}).items()]
    return [
        {
            "name": "MC Auto Translator 硬编码汉化",
            "authors": "MC Auto Translator",
            "mods": "minecraft",
            "desc": "由 MC Auto Translator 自动生成的硬编码汉化映射",
            # 修复（recheck，致命）：字段名必须是 dynamic/i18n——VaultPatcher 的
            # ModuleInfo.readJson 只认 "y"/"dyn"/"dynamic" 与 "i"/"i18n"；之前写
            # data_dynamic/data_i18n 被 skipValue 丢弃 → 模块静默落 static 模式，
            # Fabric 上硬编码汉化完全失效（用户质疑「产物适配 VP 没有」的根因）。
            "dynamic": True,
            "i18n": False,
        },
        {
            "target_classes": [],
            "pairs": pairs,
        },
    ]


def bundled_vp_jar() -> Path | None:
    """Vault Patcher all jar 路径（跨 loader + MC 版本通用，用户刚需：VP 也内置，
    整合包产物不依赖在线下载）。**优先应用目录更新版**（检查更新下载到 data/vp/，
    持久不清理），否则内置（源码 app/data/vp/；frozen _MEIPASS/app/data/vp/）。"""
    # 应用目录更新版优先
    if getattr(sys, "frozen", False):
        _up = Path(sys.executable).resolve().parent / "data" / "vp"
    else:
        _up = Path(__file__).resolve().parent.parent / "data" / "vp"
    if _up.is_dir():
        for p in sorted(_up.glob("*.jar")):
            return p
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    p = base / "app" / "data" / "vp" / "vault-patcher.jar"
    return p if p.exists() else None


async def download_vault_patcher(loader: str, mc_version: str,
                                 client: httpx.AsyncClient | None = None) -> bytes | None:
    """获取 Vault Patcher jar（用户刚需：VP 补丁内置 + 多源兜底，尽量自行下载）：
    1) **在线 Modrinth 精确匹配（loader+MC 版本）优先**——内置 all jar 的
       neoforge.mods.toml 声明 minecraft versionRange="[1.21.9,)"，对 NeoForge MC<1.21.9
       等不匹配的整合包会被 loader 拒绝加载（recheck：内置无条件优先导致静默失效）；
    2) 在线失败 → **内置 all jar 离线兜底**（fabric/forge 及大多版本可用）；
    3) GitHub latest all jar 最终兜底。
    全部失败 → 返回 None（调用方提示用户自行下载 VP mod 放入 mods/，不回退 hardcoded
    修改版 jar——用户指定整合包硬编码走 VP 形式生效）。client 可注入（测试 mock）。"""
    if not loader or not mc_version:
        # loader/版本未知（无法精确匹配）→ 直接内置 all jar（离线兜底）
        bundled = bundled_vp_jar()
        if bundled:
            try:
                return bundled.read_bytes()
            except OSError:
                pass
        return None
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=15)
    try:
        # 源 1: Modrinth（精确匹配 loader + MC 版本，优先——版本兼容性最好）
        try:
            resp = await client.get(_MODRINTH_VP_URL)
            resp.raise_for_status()
            versions = resp.json()
            for v in versions:
                if not _version_matches(mc_version, v.get("game_versions", [])):
                    continue
                if loader not in v.get("loaders", []):
                    continue
                files = v.get("files") or []
                if not files:
                    continue
                primary = next((f for f in files if f.get("primary")), files[0])
                url = primary.get("url")
                if not url:
                    continue
                dl = await client.get(url)
                dl.raise_for_status()
                if dl.content[:2] == b"PK":   # zip 有效性校验（防反代返回 HTML）
                    return dl.content
        except Exception:
            pass   # Modrinth 失败 → 内置/GitHub 兜底
        # 源 2: 内置 all jar（离线兜底，fabric/forge 大多版本可用）
        bundled = bundled_vp_jar()
        if bundled:
            try:
                data = bundled.read_bytes()
                if data[:2] == b"PK":
                    return data
            except OSError:
                pass
        # 源 3: GitHub latest release 的 all jar（跨 loader 通用兜底）
        try:
            rel = await client.get("https://api.github.com/repos/3093FengMing/VaultPatcher/releases/latest")
            rel.raise_for_status()
            for a in rel.json().get("assets", []):
                if a.get("name", "").endswith(".jar") and "all" in a["name"]:
                    dl = await client.get(a["browser_download_url"])
                    dl.raise_for_status()
                    # 修复（recheck）：GitHub 限流（403 JSON）/反代错误页会返回非 zip 内容，
                    # 之前直接 return 会把 HTML/JSON 当 VP jar 写进 mods/——与 Modrinth/内置源
                    # 一样加 PK 头校验，非 zip 跳过该 asset（无 PK 则最终返回 None 走提示）
                    if dl.content[:2] == b"PK":
                        return dl.content
        except Exception:
            pass
        logger.warning("VP 下载：%s %s 多源均无匹配/失败", loader, mc_version)
        return None
    except Exception as exc:
        # 修复：下载失败记录根因（网络/HTTP/解析），便于定位「VP 回退」的原因
        logger.warning("VP 下载失败（%s %s）：%s", loader, mc_version, exc)
        return None
    finally:
        if own:
            await client.aclose()
