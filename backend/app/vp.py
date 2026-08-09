# -*- coding: utf-8 -*-
"""Vault Patcher（VP）集成：整合包硬编码汉化的运行时替换方案。

整合包硬编码汉化：生成 VP 动态替换模块 JSON（data_dynamic 全匹配），并从 Modrinth
自动下载对应 loader + MC 版本的 VP jar。下载失败/联网不可用由 auto_flow 回退到
hardcoded 汉化 jar（替换 mod）。原整合包只读，本模块不写原包。

VP 模块文件放 vaultpatcher/modules/（新版本路径，VP 会自动从旧
config/vaultpatcher_asm 迁移）。格式参考 VPGS 社区库与官方文档，只参考字段
不抄码。
"""

import json
import re
import zipfile
from pathlib import Path

import httpx

# Modrinth 上 Vault Patcher 项目版本 API
_MODRINTH_VP_URL = "https://api.modrinth.com/v2/project/vault-patcher/version"

# MC 版本号（1.20 / 1.20.1）
_MC_VERSION_RE = re.compile(r"(1\.\d{1,2}(?:\.\d{1,2})?)")


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
    """从 mods/ 元数据推断整合包 loader + MC 版本。

    返回 (loader, mc_version)；loader ∈ {"fabric","forge","neoforge"}，
    无法推断时均为空串。遍历 mods/**/*.jar 读 fabric.mod.json /
    neoforge.mods.toml / mods.toml，取第一个命中；单个损坏 jar 跳过不中断。
    """
    if not mods_dir.is_dir():
        return ("", "")
    for jar in sorted(mods_dir.rglob("*.jar")):
        try:
            with zipfile.ZipFile(jar) as zf:
                names = zf.namelist()
                if "fabric.mod.json" in names:
                    data = json.loads(zf.read("fabric.mod.json").decode("utf-8"))
                    depends = data.get("depends") or {}
                    return ("fabric",
                            _extract_mc_version(str(depends.get("minecraft", ""))) or "")
                if "META-INF/neoforge.mods.toml" in names:
                    return ("neoforge",
                            _extract_mc_version(
                                _toml_mc_spec(zf, "META-INF/neoforge.mods.toml")) or "")
                if "META-INF/mods.toml" in names:
                    return ("forge",
                            _extract_mc_version(
                                _toml_mc_spec(zf, "META-INF/mods.toml")) or "")
        except Exception:
            continue
    return ("", "")


def build_vp_module(vp_pairs: dict[str, str]) -> list[dict]:
    """生成 VP 动态替换模块 JSON（data_dynamic 全匹配）。

    JSON 数组：模块信息头（name/authors/mods/desc/data_dynamic/data_i18n）+
    转换规则对象（target_classes 留空 → 全局动态匹配）。全匹配：value 不以 @
    开头，仅整个字符串与 key 完全相同时替换。
    """
    return [
        {
            "name": "MC Auto Translator 硬编码汉化",
            "authors": "MC Auto Translator",
            "mods": "minecraft",
            "desc": "由 MC Auto Translator 自动生成的硬编码汉化映射",
            "data_dynamic": True,
            "data_i18n": False,
        },
        {
            "target_classes": [],
            "pairs": vp_pairs,
        },
    ]


async def download_vault_patcher(loader: str, mc_version: str,
                                 client: httpx.AsyncClient | None = None) -> bytes | None:
    """从 Modrinth 下载匹配 loader + MC 版本的 Vault Patcher jar，返回 jar 字节。

    匹配失败 / 请求失败 / 网络不可用 → 返回 None（调用方回退 hardcoded 汉化 jar）。
    client 可注入（测试 mock），缺省自建并关闭。loader/版本为空直接 None，不联网。
    """
    if not loader or not mc_version:
        return None
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=15)
    try:
        resp = await client.get(_MODRINTH_VP_URL)
        resp.raise_for_status()
        versions = resp.json()
        for v in versions:
            if mc_version not in v.get("game_versions", []):
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
            return dl.content
        return None
    except Exception:
        return None
    finally:
        if own:
            await client.aclose()
