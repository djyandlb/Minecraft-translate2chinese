# -*- coding: utf-8 -*-
"""Vault Patcher 集成（vp.py）测试：VP 模块生成 + 整合包 loader/MC 版本推断 + Modrinth 下载。"""

import json
import zipfile
from pathlib import Path

import pytest

from app.vp import build_vp_module, download_vault_patcher, infer_modpack_runtime


def _make_jar(tmp_path: Path, name: str, entries: dict[str, str]) -> Path:
    """造含指定条目的 jar。"""
    jar = tmp_path / name
    with zipfile.ZipFile(jar, "w") as zf:
        for p, content in entries.items():
            zf.writestr(p, content)
    return jar


# ---------- VP 模块生成 ----------

def test_build_vp_module_format():
    """VP 模块：数组 + 模块头（data_dynamic）+ 转换规则（target_classes 留空 + pairs 全匹配）。"""
    mod = build_vp_module({"Hello World": "你好世界"})
    assert isinstance(mod, list) and len(mod) == 2
    head = mod[0]
    assert head["dynamic"] is True     # VP 官方字段名（recheck 修复：data_dynamic 被 VP 丢弃）
    assert head["i18n"] is False
    assert head["mods"] == "minecraft"
    assert mod[1]["target_classes"] == []
    # 修复：pairs 必须是键值对数组 [{key,value}]（VP 官方格式），不是 dict
    assert mod[1]["pairs"] == [{"key": "Hello World", "value": "你好世界"}]


# ---------- 整合包 loader + MC 版本推断 ----------

def test_infer_modpack_runtime_fabric(tmp_path):
    _make_jar(tmp_path, "f.jar", {
        "fabric.mod.json": json.dumps({"id": "m", "depends": {"minecraft": ">=1.20.1"}}),
    })
    loader, mc = infer_modpack_runtime(tmp_path)
    assert loader == "fabric"
    assert mc == "1.20.1"


def test_infer_modpack_runtime_forge(tmp_path):
    _make_jar(tmp_path, "f.jar", {
        "META-INF/mods.toml": (
            'modLoader="javafml"\n'
            '[[mods]]\nmodId="m"\n'
            '[[dependencies.m]]\nmodId="minecraft"\nversionRange="[1.20,1.21)"\n'
        ),
    })
    loader, mc = infer_modpack_runtime(tmp_path)
    assert loader == "forge"
    assert mc == "1.20"


def test_infer_modpack_runtime_empty(tmp_path):
    """无 mods 目录/无元数据/损坏 jar → loader 与版本均为空。"""
    assert infer_modpack_runtime(tmp_path / "mods") == ("", "")
    (tmp_path / "a.jar").write_bytes(b"notzip")
    assert infer_modpack_runtime(tmp_path) == ("", "")


# ---------- Modrinth 下载 ----------

@pytest.mark.asyncio
async def test_download_vault_patcher_success(monkeypatch):
    monkeypatch.setattr("app.vp.bundled_vp_jar", lambda: None)
    """匹配 loader + MC 版本 → 下载 primary file 字节。"""

    class _FakeResp:
        def __init__(self, data):
            self._data = data
        def raise_for_status(self):
            pass
        def json(self):
            return self._data
        @property
        def content(self):
            return self._data

    class _FakeClient:
        def __init__(self):
            self.calls = []
        async def get(self, url):
            self.calls.append(url)
            if url.endswith("/version"):
                return _FakeResp([{
                    "game_versions": ["1.19.2", "1.20.1", "1.20.2"],
                    "loaders": ["forge", "fabric", "neoforge"],
                    "files": [{"primary": True, "url": "https://x/vp.jar"}],
                }])
            return _FakeResp(b"PKvpjardata")

    client = _FakeClient()
    data = await download_vault_patcher("fabric", "1.20.1", client=client)
    assert data == b"PKvpjardata"
    assert client.calls == ["https://api.modrinth.com/v2/project/vault-patcher/version",
                            "https://x/vp.jar"]


@pytest.mark.asyncio
async def test_download_vault_patcher_no_match(monkeypatch):
    monkeypatch.setattr("app.vp.bundled_vp_jar", lambda: None)
    """版本列表不匹配（MC 版本或 loader 不符）→ 返回 None。"""

    class _FakeResp:
        def __init__(self, data):
            self._data = data
        def raise_for_status(self):
            pass
        def json(self):
            return self._data

    class _FakeClient:
        async def get(self, url):
            return _FakeResp([{
                "game_versions": ["1.19.2"],
                "loaders": ["fabric"],
                "files": [{"url": "https://x/vp.jar"}],
            }])

    assert await download_vault_patcher("fabric", "1.20.1", client=_FakeClient()) is None


@pytest.mark.asyncio
async def test_download_vault_patcher_empty_params(monkeypatch):
    monkeypatch.setattr("app.vp.bundled_vp_jar", lambda: None)
    """loader/版本为空 → 直接 None，不联网。"""
    assert await download_vault_patcher("", "") is None
    assert await download_vault_patcher("fabric", "") is None
