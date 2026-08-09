# -*- coding: utf-8 -*-
"""任务 A：全文本覆盖扫描（text_sources.py）测试。

覆盖三类文本源发现 + 写回：
  1) 语言文件（json/lang/properties，en_us → zh_cn）
  2) 结构化 JSON（递归键路径，技术串跳过）
  3) en_us 路径 txt/md（行快照保留 BOM/EOL/结构）
"""

import json
import zipfile
from pathlib import Path

from app.text_sources import discover_text_sources, write_translated


def _make_jar(tmp_path, entries: dict[str, str]) -> Path:
    """造测试 jar：entries 为 jar 内路径 → 内容（字符串以 UTF-8 写入）。"""
    jar = tmp_path / "mod.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        for p, content in entries.items():
            zf.writestr(p, content)
    return jar


def _read_jar(jar: Path, path: str) -> str:
    with zipfile.ZipFile(jar) as zf:
        return zf.read(path).decode("utf-8")


# ---------- 1) 语言文件（扩展 properties） ----------

def test_discover_lang_properties(tmp_path):
    jar = _make_jar(tmp_path, {
        "assets/mymod/lang/en_us.json": '{"k": "Hello"}',
        "assets/mymod/lang/en_us.properties": "k2=World\n",
    })
    srcs = discover_text_sources(jar)
    assert any(s.kind == "lang" and "k" in s.entries for s in srcs)
    assert any(s.kind == "lang" and "k2" in s.entries for s in srcs)


def test_discover_lang_target_path_replaces_en_us(tmp_path):
    jar = _make_jar(tmp_path, {
        "assets/mymod/lang/en_us.json": '{"k": "Hello"}',
        "assets/mymod/lang/en_us.properties": "k2=World\n",
    })
    srcs = discover_text_sources(jar)
    assert any(s.kind == "lang" and s.source_path == "assets/mymod/lang/en_us.json"
               and s.target_path == "assets/mymod/lang/zh_cn.json" for s in srcs)
    assert any(s.kind == "lang" and s.source_path == "assets/mymod/lang/en_us.properties"
               and s.target_path == "assets/mymod/lang/zh_cn.properties" for s in srcs)


def test_write_translated_lang_properties(tmp_path):
    jar = _make_jar(tmp_path, {
        "assets/mymod/lang/en_us.properties": "k2=World\n",
    })
    srcs = discover_text_sources(jar)
    props = next(s for s in srcs if s.kind == "lang" and "k2" in s.entries)
    write_translated(jar, props, {"k2": "世界"})
    assert _read_jar(jar, "assets/mymod/lang/zh_cn.properties") == "k2=世界\n"


def test_write_translated_lang_merges_existing_target(tmp_path):
    jar = _make_jar(tmp_path, {
        "assets/mymod/lang/en_us.json": '{"a": "One", "b": "Two"}',
        "assets/mymod/lang/zh_cn.json": '{"a": "一"}',
    })
    srcs = discover_text_sources(jar)
    lang = next(s for s in srcs if s.kind == "lang")
    write_translated(jar, lang, {"b": "二"})
    data = json.loads(_read_jar(jar, "assets/mymod/lang/zh_cn.json"))
    assert data == {"a": "一", "b": "二"}   # 已有译文保留，新译文写入


# ---------- 2) 结构化 JSON（递归键路径） ----------

def test_discover_structured_json_keys(tmp_path):
    jar = _make_jar(tmp_path, {
        "assets/mymod/advancement.json": json.dumps({
            "title": {"text": "Welcome"}, "hidden": "iron_ingot",
        }),
    })
    srcs = discover_text_sources(jar)
    json_src = next((s for s in srcs if s.kind == "json"), None)
    assert json_src
    assert json_src.entries.get("title.text") == "Welcome"   # 递归键路径
    assert "hidden" not in json_src.entries                    # 技术串 iron_ingot 跳过


def test_discover_json_skips_lang_dir(tmp_path):
    # assets/*/lang/ 下的 json 属于语言文件，不重复进结构化 JSON
    jar = _make_jar(tmp_path, {
        "assets/mymod/lang/en_us.json": '{"k": "Hello"}',
        "assets/mymod/advancement.json": '{"title": {"text": "Hi"}}',
    })
    srcs = discover_text_sources(jar)
    json_src = [s for s in srcs if s.kind == "json"]
    assert len(json_src) == 1
    assert json_src[0].source_path == "assets/mymod/advancement.json"


def test_discover_json_list_index_key_path(tmp_path):
    jar = _make_jar(tmp_path, {
        "assets/mymod/somedir/data.json": json.dumps({
            "pages": [{"text": "First"}, {"text": "Second"}],
        }),
    })
    srcs = discover_text_sources(jar)
    json_src = next(s for s in srcs if s.kind == "json")
    assert json_src.entries.get("pages[0].text") == "First"
    assert json_src.entries.get("pages[1].text") == "Second"


def test_write_translated_json_key_path(tmp_path):
    jar = _make_jar(tmp_path, {
        "assets/mymod/advancement.json": json.dumps({
            "title": {"text": "Welcome"}, "hidden": "iron_ingot",
        }),
    })
    srcs = discover_text_sources(jar)
    json_src = next(s for s in srcs if s.kind == "json")
    write_translated(jar, json_src, {"title.text": "欢迎"})
    data = json.loads(_read_jar(jar, "assets/mymod/advancement.json"))
    assert data == {"title": {"text": "欢迎"}, "hidden": "iron_ingot"}  # 结构保留，技术串原样


# ---------- 3) en_us 路径 txt/md（行快照） ----------

def test_discover_structured_json(tmp_path):
    jar = _make_jar(tmp_path, {
        "assets/mymod/patchouli_books/guide/en_us/entries/intro.md": "# Intro\nWelcome to the mod\n",
    })
    srcs = discover_text_sources(jar)
    # md 逐行：非空行被提取
    lines_src = next((s for s in srcs if s.kind == "lines"), None)
    assert lines_src and "Welcome to the mod" in lines_src.entries.values()
    assert lines_src.target_path == "assets/mymod/patchouli_books/guide/zh_cn/entries/intro.md"


def test_write_translated_lines_preserves_structure(tmp_path):
    jar = _make_jar(tmp_path, {
        "assets/mymod/patchouli_books/guide/en_us/entries/intro.md": "# Intro\r\nWelcome to the mod\r\n",
    })
    srcs = discover_text_sources(jar)
    lines_src = next((s for s in srcs if s.kind == "lines"), None)
    assert lines_src
    # 把 "Welcome to the mod" 行翻译
    key = next(k for k, v in lines_src.entries.items() if v == "Welcome to the mod")
    write_translated(jar, lines_src, {key: "欢迎来到本模组"})
    content = _read_jar(jar, lines_src.target_path)
    assert "欢迎来到本模组" in content
    assert "# Intro" in content          # 结构保留
    assert content.endswith("\r\n")      # EOL 保留


def test_lines_snapshot_keeps_bom_eol_trailing(tmp_path):
    # BOM + CRLF + trailing newline 全部保留
    jar = _make_jar(tmp_path, {
        "assets/mymod/patchouli_books/guide/en_us/entries/intro.md": "﻿# Intro\r\nWelcome\r\n",
    })
    srcs = discover_text_sources(jar)
    lines_src = next(s for s in srcs if s.kind == "lines")
    assert lines_src.line_snapshot == {
        "bom": True, "eol": "\r\n", "trailing_newline": True, "line_count": 2,
    }
    key = next(k for k, v in lines_src.entries.items() if v == "Welcome")
    write_translated(jar, lines_src, {key: "欢迎"})
    content = _read_jar(jar, lines_src.target_path)
    assert content == "﻿# Intro\r\n欢迎\r\n"


def test_write_translated_lines_blank_lines_preserved(tmp_path):
    # 空行不提取为条目，但写回后空行结构保留
    jar = _make_jar(tmp_path, {
        "assets/mymod/patchouli_books/g/en_us/entries/x.md": "Para one\n\nPara two\n",
    })
    srcs = discover_text_sources(jar)
    lines_src = next(s for s in srcs if s.kind == "lines")
    assert len(lines_src.entries) == 2           # 空行不提取
    key = next(k for k, v in lines_src.entries.items() if v == "Para two")
    write_translated(jar, lines_src, {key: "第二段"})
    content = _read_jar(jar, lines_src.target_path)
    assert content == "Para one\n\n第二段\n"      # 空行保留


# ---------- 整体：排序 / 容错 ----------

def test_discover_sorted_by_kind_then_modid(tmp_path):
    jar = _make_jar(tmp_path, {
        "assets/bmod/lang/en_us.json": '{"k": "B"}',
        "assets/amod/lang/en_us.json": '{"k": "A"}',
        "assets/amod/patchouli_books/g/en_us/entries/x.md": "Hello\n",
        "assets/amod/advancement.json": '{"title": {"text": "T"}}',
    })
    srcs = discover_text_sources(jar)
    kinds = [s.kind for s in srcs]
    # lang < json < lines（字母序），同 kind 按 modid 升序
    assert kinds == sorted(kinds)
    for k in ("lang", "json", "lines"):
        sub = [s.modid for s in srcs if s.kind == k]
        assert sub == sorted(sub)


def test_discover_ignores_zip_slip_entry(tmp_path):
    # 恶意条目（../ 逃逸）不落盘、不崩；正常文件照常发现
    jar = tmp_path / "evil.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("assets/mymod/lang/en_us.json", '{"k": "Hi"}')
        zf.writestr("../../evil.txt", "pwned")
    srcs = discover_text_sources(jar)
    assert any(s.kind == "lang" for s in srcs)


# ---------- modjar 单一汉化 jar：语言文件写回（write_lang_into_jar） ----------

def test_write_lang_into_jar_new_lang_file(tmp_path):
    """modjar 语言文件写回：pack_format 3（1.12-）→ zh_cn.lang 新建。"""
    from app.text_sources import write_lang_into_jar
    jar = _make_jar(tmp_path, {
        "assets/mymod/lang/en_us.lang": "key.hello=Hello World\n",
    })
    write_lang_into_jar(jar, {"mymod": {"key.hello": "你好世界"}}, "zh_cn", 3)
    assert _read_jar(jar, "assets/mymod/lang/zh_cn.lang") == "key.hello=你好世界\n"


def test_write_lang_into_jar_merges_existing_zh(tmp_path):
    """modjar 语言文件写回：已有 zh_cn.json → 合并保留已有条目 + 覆盖/新增译文。"""
    from app.text_sources import write_lang_into_jar
    jar = _make_jar(tmp_path, {
        "assets/mymod/lang/en_us.json": '{"key.hello": "Hello World", "key.bye": "Bye"}',
        "assets/mymod/lang/zh_cn.json": '{"key.bye": "再见"}',
    })
    write_lang_into_jar(jar, {"mymod": {"key.hello": "你好世界", "key.bye": "告别"}}, "zh_cn", 15)
    data = json.loads(_read_jar(jar, "assets/mymod/lang/zh_cn.json"))
    assert data == {"key.hello": "你好世界", "key.bye": "告别"}


def test_write_lang_into_jar_skips_bad_modid(tmp_path):
    """modjar 语言文件写回：恶意 modid（含 ../）跳过，不产生路径穿越文件。"""
    from app.text_sources import write_lang_into_jar
    jar = _make_jar(tmp_path, {
        "assets/mymod/lang/en_us.json": '{"key.hello": "Hello World"}',
    })
    write_lang_into_jar(jar, {"../evil": {"k": "v"}}, "zh_cn", 15)
    with zipfile.ZipFile(jar) as zf:
        names = zf.namelist()
    assert not any("../" in n or "evil" in n for n in names)


# ---------- 整合包目录文本源（任务线 / config / data / kubejs） ----------

from app.text_sources import discover_pack_text_sources, render_pack_source


def _make_pack(tmp_path, files: dict[str, str]) -> Path:
    """造整合包目录：files 为相对路径 → 内容（UTF-8 字节写入，避免 Windows 换行转换）。"""
    pack = tmp_path / "pack"
    for rel, content in files.items():
        p = pack / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode("utf-8"))
    return pack


def test_discover_pack_ftbquests_json(tmp_path):
    """任务线 config/ftbquests/*.json：递归字符串值，技术串（minecraft:stone）跳过。"""
    pack = _make_pack(tmp_path, {
        "config/ftbquests/quests/1.json": json.dumps({
            "title": "Welcome", "description": "Do quests", "item": "minecraft:stone",
        }),
    })
    srcs = discover_pack_text_sources(pack)
    quest = next(s for s in srcs if s.source_path == "config/ftbquests/quests/1.json")
    assert quest.kind == "json"
    assert quest.entries["title"] == "Welcome"
    assert quest.entries["description"] == "Do quests"
    assert "item" not in quest.entries            # 命名空间技术串跳过


def test_discover_pack_data_json(tmp_path):
    """data/ 数据包 json：递归 key_path 发现，source_path = 整合包相对路径。"""
    pack = _make_pack(tmp_path, {
        "data/demo/advancement/title.json": json.dumps({
            "title": {"text": "New World"}, "item": {"id": "minecraft:stone"},
        }),
    })
    srcs = discover_pack_text_sources(pack)
    assert any(s.source_path == "data/demo/advancement/title.json"
               and s.entries.get("title.text") == "New World" for s in srcs)


def test_discover_pack_kubejs_js(tmp_path):
    """kubejs/scripts js 字符串字面量提取，技术串（minecraft:stone 事件名）过滤。"""
    pack = _make_pack(tmp_path, {
        "kubejs/server_scripts/main.js": 'onEvent("recipes", e => console.log("Hello World"))',
        "scripts/x.js": 'Item.of("minecraft:stone", "Hi")',
    })
    srcs = discover_pack_text_sources(pack)
    js = next(s for s in srcs if s.source_path == "kubejs/server_scripts/main.js")
    assert "Hello World" in js.entries.values()    # 字符串字面量内容被提取
    assert "minecraft:stone" not in js.entries.values()   # 命名空间技术串跳过


def test_discover_pack_toml_and_properties(tmp_path):
    """config/ toml + properties 字符串值发现，开关字面量（true）跳过。"""
    pack = _make_pack(tmp_path, {
        "config/example.toml": 'name = "Welcome"\nenabled = true\n',
        "config/example.properties": "title=Welcome to the mod\nenabled=true\n",
    })
    srcs = discover_pack_text_sources(pack)
    toml = next(s for s in srcs if s.source_path == "config/example.toml")
    assert "Welcome" in toml.entries.values()
    assert "true" not in toml.entries.values()     # 开关值跳过，不翻译坏配置
    props = next(s for s in srcs if s.source_path == "config/example.properties")
    assert "Welcome to the mod" in props.entries.values()
    assert "true" not in props.entries.values()


def test_discover_pack_source_path_relative(tmp_path):
    """目录文本源 source_path 均为整合包相对路径（不含包根前缀），供补丁包按相对路径组织。"""
    pack = _make_pack(tmp_path, {
        "config/a.json": '{"t": "Hi"}',
        "data/ns/b.json": '{"t": "Hi"}',
    })
    srcs = discover_pack_text_sources(pack)
    for s in srcs:
        assert not s.source_path.startswith(("pack/", "\\", "/"))


def test_discover_pack_does_not_modify_original(tmp_path):
    """原整合包只读：发现阶段不写任何文件。"""
    pack = _make_pack(tmp_path, {"config/a.json": '{"title": "Welcome"}'})
    before = (pack / "config/a.json").read_bytes()
    discover_pack_text_sources(pack)
    assert (pack / "config/a.json").read_bytes() == before


def test_render_pack_json_keeps_structure(tmp_path):
    """目录 json 渲染：按 key_path 覆写译文，技术串与结构保留。"""
    pack = _make_pack(tmp_path, {
        "config/ftbquests/quests/1.json": json.dumps({
            "title": "Welcome", "item": "minecraft:stone",
        }),
    })
    srcs = discover_pack_text_sources(pack)
    quest = next(s for s in srcs if s.source_path == "config/ftbquests/quests/1.json")
    out = render_pack_source(quest, {"title": "欢迎"}, pack)
    assert json.loads(out) == {"title": "欢迎", "item": "minecraft:stone"}


def test_render_pack_kubejs_inline_replace(tmp_path):
    """目录 js 渲染：字符串字面量原位替换，保留周围代码结构。"""
    pack = _make_pack(tmp_path, {
        "kubejs/server_scripts/main.js": 'onEvent("recipes", e => console.log("Hello World"))\n',
    })
    srcs = discover_pack_text_sources(pack)
    js = next(s for s in srcs if s.source_path == "kubejs/server_scripts/main.js")
    key = next(k for k, v in js.entries.items() if v == "Hello World")
    out = render_pack_source(js, {key: "你好世界"}, pack)
    assert 'console.log("你好世界")' in out
    assert 'onEvent("recipes"' in out            # 未翻译字面量原样保留
    assert out.endswith("\n")                    # EOL 保留


def test_render_pack_toml_and_properties_preserve_key(tmp_path):
    """toml/properties 渲染：只替换值，保留 key= 前缀与引号。"""
    pack = _make_pack(tmp_path, {
        "config/example.toml": 'name = "Welcome"\n',
        "config/example.properties": "title=Welcome\n",
    })
    srcs = discover_pack_text_sources(pack)
    toml = next(s for s in srcs if s.source_path == "config/example.toml")
    tkey = next(k for k, v in toml.entries.items() if v == "Welcome")
    assert render_pack_source(toml, {tkey: "欢迎"}, pack) == 'name = "欢迎"\n'
    props = next(s for s in srcs if s.source_path == "config/example.properties")
    pkey = next(k for k, v in props.entries.items() if v == "Welcome")
    assert render_pack_source(props, {pkey: "欢迎"}, pack) == "title=欢迎\n"
