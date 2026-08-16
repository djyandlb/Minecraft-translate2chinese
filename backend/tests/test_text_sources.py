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

from app.text_sources import discover_text_sources, write_lang_into_jar, write_translated


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


def test_discover_lang_uppercase_file(tmp_path):
    # en_US.json（大写）被识别并发现：lang 统一小写后匹配 en_us
    jar = _make_jar(tmp_path, {
        "assets/mymod/lang/en_US.json": '{"k": "Hello"}',
    })
    srcs = discover_text_sources(jar)
    assert any(s.kind == "lang" and s.source_path == "assets/mymod/lang/en_US.json"
               and s.entries.get("k") == "Hello" for s in srcs)


def test_discover_lang_snake_case_value(tmp_path):
    # 语言文件值 Requires_Armor（snake_case 形态真实短语）进入 entries
    jar = _make_jar(tmp_path, {
        "assets/mymod/lang/en_us.json": json.dumps({"item.armor": "Requires_Armor"}),
    })
    srcs = discover_text_sources(jar)
    lang = next(s for s in srcs if s.kind == "lang")
    assert lang.entries.get("item.armor") == "Requires_Armor"


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


def test_write_lang_into_jar_aligns_en_us_keys(tmp_path):
    """Xaero 审查修复：合并已有 zh_cn 时删除 en_us 中不存在的旧残留键（多 5 键根因）。
    旧 zh_cn 里 en_us 没有的键（跨版本合并事故残留）应删除，不能混进产物。"""
    jar = _make_jar(tmp_path, {
        "assets/mymod/lang/en_us.json": '{"a": "One", "b": "Two"}',
        # 旧 zh_cn：a 已汉化、c 是 en_us 没有的残留键（事故残留）
        "assets/mymod/lang/zh_cn.json": '{"a": "一", "c": "幽灵键"}',
    })
    write_lang_into_jar(jar, {"mymod": {"b": "二"}}, "zh_cn", 15)
    data = json.loads(_read_jar(jar, "assets/mymod/lang/zh_cn.json"))
    # a（en_us 有、已汉化）保留；b（新译）并入；c（en_us 无）删除
    assert data == {"a": "一", "b": "二"}, f"对齐失败: {data}"
    assert "c" not in data


# ---------- 2) 结构化 JSON（递归键路径，回归标准：只翻文本载体） ----------

def test_discover_structured_json_keys(tmp_path):
    # 回归标准：只翻「明确文本载体」——Patchouli 教程书（patchouli_books + en_us 段）
    jar = _make_jar(tmp_path, {
        "assets/mymod/patchouli_books/guide/en_us/entries/test.json": json.dumps({
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
        "assets/mymod/patchouli_books/guide/en_us/entries/test.json": '{"title": {"text": "Hi"}}',
    })
    srcs = discover_text_sources(jar)
    json_src = [s for s in srcs if s.kind == "json"]
    assert len(json_src) == 1
    assert json_src[0].source_path == "assets/mymod/patchouli_books/guide/en_us/entries/test.json"


def test_discover_json_list_index_key_path(tmp_path):
    jar = _make_jar(tmp_path, {
        "assets/mymod/patchouli_books/guide/en_us/entries/test.json": json.dumps({
            "pages": [{"text": "First"}, {"text": "Second"}],
        }),
    })
    srcs = discover_text_sources(jar)
    json_src = next(s for s in srcs if s.kind == "json")
    assert json_src.entries.get("pages[0].text") == "First"
    assert json_src.entries.get("pages[1].text") == "Second"


def test_write_translated_json_key_path(tmp_path):
    jar = _make_jar(tmp_path, {
        "assets/mymod/patchouli_books/guide/en_us/entries/test.json": json.dumps({
            "title": {"text": "Welcome"}, "hidden": "iron_ingot",
        }),
    })
    srcs = discover_text_sources(jar)
    json_src = next(s for s in srcs if s.kind == "json")
    write_translated(jar, json_src, {"title.text": "欢迎"})
    # 写回目标是 zh_cn 路径（en_us → zh_cn 替换），原 en_us 文件不动
    data = json.loads(_read_jar(jar, "assets/mymod/patchouli_books/guide/zh_cn/entries/test.json"))
    assert data == {"title": {"text": "欢迎"}, "hidden": "iron_ingot"}  # 结构保留，技术串原样
    assert _read_jar(jar, "assets/mymod/patchouli_books/guide/en_us/entries/test.json")  # 源文件仍在


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


def test_discover_pack_ftbquests_snbt(tmp_path):
    """FTB Quests 任务书 .snbt：提取 title/description 数组字符串，id 等结构字段不动。"""
    from app.text_sources import discover_pack_text_sources, render_pack_source
    pack = _make_pack(tmp_path, {
        "config/ftbquests/quests/ch1.snbt":
            'title: "First Quest"\ndescription: ["Find the diamond", "And the emerald"]\nid: "abc123"\n',
    })
    srcs = discover_pack_text_sources(pack)
    snbt = next(s for s in srcs if s.source_path.endswith(".snbt"))
    assert snbt.kind == "lines"
    assert "First Quest" in snbt.entries.values()
    assert "Find the diamond" in snbt.entries.values()
    assert "And the emerald" in snbt.entries.values()
    # 写回：翻译 title/description，id 结构字段保留
    trans = {k: v + "【译】" for k, v in snbt.entries.items()}
    out = render_pack_source(snbt, trans, pack)
    assert 'title: "First Quest【译】"' in out
    assert '"Find the diamond【译】"' in out
    assert '"And the emerald【译】"' in out
    assert 'id: "abc123"' in out


def test_discover_pack_data_json(tmp_path):
    """data/ 数据包 json：advancements 只翻 display.title/description 文本（白名单），
    不收 criteria 触发条件 / icon 资源字段（recheck：criteria.throw_ring_in_lava 被翻成「这个」）。"""
    pack = _make_pack(tmp_path, {
        "data/demo/advancements/title.json": json.dumps({
            "criteria": {"impossible": {"trigger": "minecraft:impossible"}},
            "display": {
                "title": {"text": "New World"},
                "description": {"text": "Enter a new world"},
                "icon": {"item": "minecraft:stone"},
            },
        }),
        "data/demo/recipes/craft.json": json.dumps({"result": "minecraft:stone"}),
    })
    srcs = discover_pack_text_sources(pack)
    adv = next(s for s in srcs if s.source_path == "data/demo/advancements/title.json")
    assert adv.entries.get("display.title.text") == "New World"
    assert adv.entries.get("display.description.text") == "Enter a new world"
    assert not any(k.startswith("criteria") for k in adv.entries), "criteria 触发条件不收"
    assert not any(k.startswith("display.icon") for k in adv.entries), "icon 资源字段不收"
    # 配方等非文本载体 json 不翻（回归标准）
    assert not any("recipes" in s.source_path for s in srcs)


def test_discover_pack_kubejs_js_skipped(tmp_path):
    """回归标准：kubejs/scripts 的 js 是代码（字符串含事件名/资源 id），不翻。
    kubejs 汉化走 lang 文件（kubejs/assets/*/lang/），脚本字符串不收集。"""
    pack = _make_pack(tmp_path, {
        "kubejs/server_scripts/main.js": 'onEvent("recipes", e => console.log("Hello World"))',
        "scripts/x.js": 'Item.of("minecraft:stone", "Hi")',
    })
    srcs = discover_pack_text_sources(pack)
    assert not any(s.source_path.endswith(".js") for s in srcs)   # js 一律不收集


def test_discover_pack_config_params_skipped(tmp_path):
    """回归标准：config/ 的 toml/properties/json 是配置参数（开关/坐标），不翻。
    只有 ftbquests/betterquesting 任务书走 config 白名单。"""
    pack = _make_pack(tmp_path, {
        "config/example.toml": 'name = "Welcome"\nenabled = true\n',
        "config/example.properties": "title=Welcome to the mod\nenabled=true\n",
        "config/example.json": '{"mode": "linear"}',
        "config/ftbquests/quests/chapter1.json": '{"title": "Quest Title"}',
    })
    srcs = discover_pack_text_sources(pack)
    assert not any(s.source_path == "config/example.toml" for s in srcs)
    assert not any(s.source_path == "config/example.properties" for s in srcs)
    assert not any(s.source_path == "config/example.json" for s in srcs)
    # ftbquests 任务书是白名单文本载体 → 收集
    assert any(s.source_path == "config/ftbquests/quests/chapter1.json" for s in srcs)


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


def test_render_jar_sources_batch(tmp_path):
    """批量渲染一次解压 jar（修复 O(n²) 卡住：原 render_jar_source 逐源全量解压+删除 jar，
    大 jar 教程书几十个文本源卡在「正在写入…(1/N)」）。"""
    import zipfile
    import json as _json
    from app.text_sources import render_jar_sources_batch, TextSource
    jar = tmp_path / "mod.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("assets/mymod/patchouli_books/guide/en_us/entries/a.json",
                    _json.dumps({"name": "Hello", "text": "Welcome"}))
        zf.writestr("assets/mymod/lang/en_us.json",
                    _json.dumps({"key.a": "Alpha"}))
    src1 = TextSource(kind="json", modid="mymod",
                      source_path="assets/mymod/patchouli_books/guide/en_us/entries/a.json",
                      target_path="assets/mymod/patchouli_books/guide/zh_cn/entries/a.json",
                      entries={"name": "Hello"})
    src2 = TextSource(kind="lang", modid="mymod",
                      source_path="assets/mymod/lang/en_us.json",
                      target_path="assets/mymod/lang/zh_cn.json",
                      entries={"key.a": "Alpha"})
    out = render_jar_sources_batch(jar, [(src1, {"name": "你好"}), (src2, {"key.a": "阿尔法"})])
    assert len(out) == 2, f"应渲染 2 个文本源，实际 {len(out)}"
    by_path = dict(out)
    assert "你好" in by_path["assets/mymod/patchouli_books/guide/zh_cn/entries/a.json"]
    assert "阿尔法" in by_path["assets/mymod/lang/zh_cn.json"]


def test_discover_pack_kubejs_lang_target_zh_cn(tmp_path):
    """KubeJS 补语言文件（kubejs/assets/*/lang/en_us.json）target_path 应替换为 zh_cn——
    v1.2.0 修复：整合包目录文本源产物写入用 target_path，否则译文覆盖 en_us 原文件、
    游戏按目标语言读 zh_cn 永远看不到（用户实测 KubeJS 物品 + FTB 任务翻译键未汉化）。
    注意 kubejs/assets 的 lang 走 _is_pack_text_carrier（json 源），非 _pack_js_source。"""
    from app.text_sources import discover_pack_text_sources
    js = tmp_path / "kubejs" / "assets" / "kubejs" / "lang" / "en_us.json"
    js.parent.mkdir(parents=True)
    js.write_text('{"item.kubejs.zahraanite": "Zahraanite", '
                  '"item.kubejs.zeno_sword": "Zeno Sword"}', encoding="utf-8")
    srcs = discover_pack_text_sources(tmp_path)
    lang_srcs = [s for s in srcs if "kubejs/assets/kubejs/lang" in s.source_path]
    assert lang_srcs, "应发现 kubejs 补语言文件"
    src = lang_srcs[0]
    assert src.target_path == "kubejs/assets/kubejs/lang/zh_cn.json", \
        f"target 应为 zh_cn，实际 {src.target_path}"
    assert "item.kubejs.zahraanite" in src.entries


def test_pack_text_carrier_accepts_plain_assets_lang():
    """v1.2.0 全量覆盖：普通目录 assets/<mod>/lang（非只 kubejs）也收为文本载体——
    （用户实测：整合包自定义资源包 lang、FTBQ 翻译键 ftbquestlocalizer 等漏翻）。"""
    from app.text_sources import _is_pack_text_carrier
    assert _is_pack_text_carrier("assets/mymod/lang/en_us.json")
    assert _is_pack_text_carrier("assets/kubejs/lang/en_us.json")
    assert _is_pack_text_carrier("assets/ftbquestlocalizer/lang/en_us.json")
    assert not _is_pack_text_carrier("config/mymod/settings.json")
    assert not _is_pack_text_carrier("data/mymod/recipes/x.json")


def test_guide_md_mirror_target():
    """AE2 指南 md（assets/ae2/ae2guide/*.md，无 en_us 段）识别 + target 走 _zh_cn 镜像
    （修复 recheck：GuideME 指南 md 无 en_us 路径段被 _has_en_us_segment 跳过 → 未汉化）。"""
    from app.text_sources import _is_guide_md, _guide_mirror_path
    assert _is_guide_md("assets/ae2/ae2guide/getting-started.md")
    assert _is_guide_md("assets/ae2/ae2guide/mechanics/import-export.md")
    assert not _is_guide_md("assets/mymod/lang/en_us.json")
    assert not _is_guide_md("assets/ae2/ae2guide/foo.png")
    assert not _is_guide_md("data/mymod/recipes/x.json")
    assert _guide_mirror_path("assets/ae2/ae2guide/getting-started.md", "zh_cn") == \
        "assets/ae2/ae2guide/_zh_cn/getting-started.md"
    assert _guide_mirror_path("assets/ae2/ae2guide/mechanics/x.md", "zh_tw") == \
        "assets/ae2/ae2guide/_zh_tw/mechanics/x.md"


def test_pack_js_tooltip_event_add(tmp_path):
    """KubeJS 官方 tooltip API：ItemEvents.tooltip + event.add('id', [...]) 提取
    （修复 recheck 🔴：原字段白名单无 add，官方 API 整段漏收）。"""
    from app.text_sources import discover_pack_text_sources
    js = tmp_path / "kubejs" / "client_scripts" / "tooltips.js"
    js.parent.mkdir(parents=True)
    js.write_text(
        "ItemEvents.tooltip(event => {\n"
        "  event.add('minecraft:diamond', ['First line', 'Second line'])\n"
        "})\n", encoding="utf-8")
    srcs = discover_pack_text_sources(tmp_path)
    js_srcs = [s for s in srcs if "tooltips.js" in s.source_path]
    assert js_srcs, "应发现 KubeJS tooltip 脚本"
    vals = js_srcs[0].entries.values()
    assert "First line" in vals and "Second line" in vals
    assert "minecraft:diamond" not in vals   # id 技术串不提取


def test_pack_js_multiline_array_all_elements(tmp_path):
    """KubeJS 跨行数组多元素全提取（修复 recheck 🔴：原状态机在中间元素行提前退出，
    跨行 tooltip 只收首元素/前两个，后续漏收）。"""
    from app.text_sources import discover_pack_text_sources
    js = tmp_path / "kubejs" / "client_scripts" / "tips.js"
    js.parent.mkdir(parents=True)
    js.write_text(
        "const tips = {\n"
        "  tooltip: ['Aaa',\n"
        "      'Bbb',\n"
        "      'Ccc']\n"
        "}\n", encoding="utf-8")
    srcs = discover_pack_text_sources(tmp_path)
    js_srcs = [s for s in srcs if "tips.js" in s.source_path]
    assert js_srcs
    vals = list(js_srcs[0].entries.values())
    assert "Aaa" in vals and "Bbb" in vals and "Ccc" in vals, \
        f"跨行数组应全收，实际 {vals}"


def test_guide_lines_skips_frontmatter_component():
    """GuideME 指南行提取：跳过 front matter 键行与组件标签行，只翻正文
    （修复 recheck 🔴：原整行翻译会把 title:/<ItemLink> 改坏，AE2 指南解析失败）。"""
    from app.text_sources import _guide_lines_entries
    lines = ["---", "title: Getting Started", "parent: index.md",
             '<ItemLink id="ae2:controller" />', "Hello world", "---"]
    entries = _guide_lines_entries(lines)
    vals = entries.values()
    assert "Hello world" in vals
    assert "title: Getting Started" not in vals
    assert '<ItemLink id="ae2:controller" />' not in vals
    assert "parent: index.md" not in vals


def test_guide_lines_skips_markdown_structure_lines():
    """v1.4.0（用户「AE2 指南翻译失败骤增 + [Subnetworksl../ae2- 碎片 + 一页横线」）：
    GuideME 行提取跳过 markdown 结构行——纯导航链接/表格/YAML 分隔线/引用定义。
    这些是结构不是内容，逐行翻译低配模型必然破坏（链接 ]( 变 l、表格被拆、分隔线
    成横线），整行跳过（正文段落仍收集——正文内嵌链接由 placeholder 整链接保护）。"""
    from app.text_sources import _guide_lines_entries
    lines = [
        "---",                                   # YAML 分隔线
        "title: Wireless Terminals",
        "parent: items-blocks-machines",
        "---",
        "",
        "# Wireless Terminals",                  # 标题：保留
        "Wireless terminals allow access.",      # 正文：保留
        "[Subnetworks](../ae2-mechanics/subnetworks.md)",       # 纯导航链接：跳过
        "[P2P Tunnels](../ae2-mechanics/machines/p2p_tunnels.md)",  # 纯导航链接：跳过
        "---",                                   # 正文分隔线：跳过
        "| Item | Cost |",                       # 表格：跳过
        "|------|------|",                       # 表格分隔：跳过
        "| Wire | 1 |",                          # 表格数据：跳过
        "- [IO Port](machines/io_port.md)",      # 列表链接：跳过
        "* [Level Emitter](machines/level_emitter.md)",  # 列表链接：跳过
        "[ref]: https://example.com",            # 引用定义：跳过
        "See [Wireless Access](wireless_access.md) for details.",  # 正文内嵌链接：保留（placeholder 保结构）
    ]
    entries = _guide_lines_entries(lines)
    vals = entries.values()
    assert "# Wireless Terminals" in vals
    assert "Wireless terminals allow access." in vals
    assert "See [Wireless Access](wireless_access.md) for details." in vals
    for skip in [
        "[Subnetworks](../ae2-mechanics/subnetworks.md)",
        "[P2P Tunnels](../ae2-mechanics/machines/p2p_tunnels.md)",
        "| Item | Cost |", "|------|------|", "| Wire | 1 |",
        "- [IO Port](machines/io_port.md)", "* [Level Emitter](machines/level_emitter.md)",
        "[ref]: https://example.com",
    ]:
        assert skip not in vals, f"结构行不该收集：{skip}"
    # 纯链接整行只有一个元素时（无正文混合），历次收集不应有 '---' 分隔线单独成条
    assert "---" not in vals


def test_pack_text_carrier_only_en_us_lang():
    """整合包目录 lang 全收（v1.3.9 修复「FTB/KubeJS 没全量翻译」）：含只有 zh_cn 无 en_us
    的 mod（ftbquestlocalizer 英文占位）——否则纯英文占位漏翻。已汉化值由
    _has_target_chars / 值过滤跳过（不覆盖 mod 自带多语言）。"""
    from app.text_sources import _is_pack_text_carrier, _zh_cn_path, _has_target_chars
    assert _is_pack_text_carrier("assets/mymod/lang/en_us.json")
    assert _is_pack_text_carrier("assets/mymod/lang/en_US.json")
    # v1.3.9：zh_cn/de_de 也收（FTB localizer 只有 zh_cn 英文占位），值级过滤判已汉化
    assert _is_pack_text_carrier("assets/mymod/lang/zh_cn.json")
    assert _is_pack_text_carrier("assets/mymod/lang/de_de.json")
    assert _zh_cn_path("assets/mymod/lang/en_US.json", "zh_cn") == \
        "assets/mymod/lang/zh_cn.json"
    # 值级判定：英文占位该翻，已汉化跳过
    assert not _has_target_chars("ATM Alloy tools", "zh_cn")
    assert _has_target_chars("钢铁合金工具", "zh_cn")


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


def test_render_pack_quest_json_inline(tmp_path):
    """目录文本载体 json 渲染：按 key_path 覆写译文，技术串与结构保留（ftbquests 任务书）。"""
    pack = _make_pack(tmp_path, {
        "config/ftbquests/quests/1.json": json.dumps({
            "title": "Welcome", "item": "minecraft:stone",
        }),
    })
    srcs = discover_pack_text_sources(pack)
    quest = next(s for s in srcs if s.source_path == "config/ftbquests/quests/1.json")
    out = render_pack_source(quest, {"title": "欢迎"}, pack)
    assert json.loads(out) == {"title": "欢迎", "item": "minecraft:stone"}


def test_advancement_only_display_text(tmp_path):
    """advancements 只收 display.title/description 文本，不收 criteria 触发条件（代码逻辑）。

    recheck 修复：criteria.throw_ring_in_lava 这类触发条件被当文本翻译（用户实测翻成「这个」），
    翻译会破坏进度判定。白名单限定 display.title/description。
    """
    from app.text_sources import discover_text_sources
    jar = tmp_path / "m.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("data/mymod/advancements/t.json", json.dumps({
            "criteria": {
                "throw_ring_in_lava": {
                    "conditions": {"item": {"items": ["minecraft:stick"]}},
                    "trigger": "minecraft:item_used_on_block",
                },
            },
            "display": {
                "title": {"text": "A Ring in the Lava"},
                "description": {"text": "Throw the ring into lava."},
            },
        }))
    srcs = discover_text_sources(jar)
    adv = next(s for s in srcs if s.kind == "json")
    assert "display.title.text" in adv.entries
    assert "display.description.text" in adv.entries
    assert not any(k.startswith("criteria") for k in adv.entries), \
        f"criteria 触发条件不应被收集: {[k for k in adv.entries if k.startswith('criteria')]}"
    assert not any(k.startswith("display.title.text") for k in adv.entries if False)  # no-op


def test_pack_snbt_ftb_new_format_lang(tmp_path):
    """FTB Quests 新格式（1.20+）quests/lang/en_us.snbt：提取 quest.<id>.title / quest_desc
    数组 / chapter.title——旧正则只认 description 漏翻 quest_desc 描述（recheck：任务书没翻译根因）。"""
    from app.text_sources import discover_pack_text_sources
    pack = _make_pack(tmp_path, {
        "config/ftbquests/quests/lang/en_us.snbt": (
            'quest.00FD.title: "Draconic Bee"\n'
            'quest.00FD.quest_subtitle: "&9Requires Nest"\n'
            'quest.00FD.quest_desc: ["Place an Obsidian Nest", "Catch a bee"]\n'
            'chapter.0721.title: "Applied Energistics"\n'
        ),
    })
    srcs = discover_pack_text_sources(pack)
    snbt = next(s for s in srcs if s.kind == "lines")
    vals = snbt.entries.values()
    assert "Draconic Bee" in vals
    assert "&9Requires Nest" in vals   # 格式码 &9 原样保留提取
    assert "Place an Obsidian Nest" in vals, "quest_desc 数组元素应被提取（旧正则漏翻）"
    assert "Catch a bee" in vals
    assert "Applied Energistics" in vals, "chapter.title 应被提取"
    assert not any(v == "00FD" for v in vals), "id 结构字段不收集"


def test_pack_js_source_extracts_text_fields(tmp_path):
    """KubeJS 脚本：只提取 text/title/tooltip 等文本字段（含数组元素），
    代码串（事件名/路径/命令/modid:key/非白名单字段）跳过，行内替换保留脚本结构。"""
    from app.text_sources import discover_pack_text_sources, render_pack_source
    js = tmp_path / "kubejs" / "server_scripts" / "gui.js"
    js.parent.mkdir(parents=True)
    js.write_text(
        "onEvent('server.recipes', event => {\n"
        "  event.shaped('3x minecraft:stone', ['AA'], { A: 'minecraft:cobblestone' })\n"
        "  const path = 'kubejs/data/x'\n"
        "})\n"
        "let gui = {\n"
        "  text: 'Hello World',\n"
        "  title: \"Welcome\",\n"
        "  tooltip: ['First line', 'Second line'],\n"
        "  sound: 'minecraft:block.anvil.hit',\n"
        "  cmd: '/give @p diamond',\n"
        "}\n",
        encoding="utf-8")
    srcs = [s for s in discover_pack_text_sources(tmp_path)
            if s.kind == "lines" and "gui.js" in s.source_path]
    assert srcs, "应发现 kubejs 脚本文本源"
    src = srcs[0]
    vals = src.entries.values()
    assert "Hello World" in vals and "Welcome" in vals
    assert "First line" in vals and "Second line" in vals
    # 代码串 / 非文本字段不收集
    assert "server.recipes" not in vals
    assert "3x minecraft:stone" not in vals
    assert "kubejs/data/x" not in vals
    assert "minecraft:block.anvil.hit" not in vals    # sound 非文本字段
    assert "/give @p diamond" not in vals            # cmd 非文本字段
    # 行内替换保留脚本结构（含引号与格式）
    out = render_pack_source(src, {k: "你好世界" for k in src.entries}, tmp_path)
    assert "text: '你好世界'" in out
    assert "title: \"你好世界\"" in out
    assert "tooltip: ['你好世界', '你好世界']" in out
    assert "onEvent('server.recipes'" in out         # 代码串原样
    assert "'3x minecraft:stone'" in out


def test_pack_js_source_skips_register_names(tmp_path):
    """KubeJS 注册脚本：物品**显示名** displayName('My Ingot') 是用户可见文本必须提取
    （修复 recheck：displayName 驼峰漏出白名单 → 整个脚本不进翻译，KubeJS 物品名未汉化），
    注册名 'my_ingot'（create 字段，代码标识）不提取。"""
    from app.text_sources import discover_pack_text_sources, render_pack_source
    js = tmp_path / "kubejs" / "startup_scripts" / "registry.js"
    js.parent.mkdir(parents=True)
    js.write_text(
        "onEvent('item.registry', event => {\n"
        "  event.create('my_ingot').displayName('My Ingot')\n"
        "})\n",
        encoding="utf-8")
    srcs = discover_pack_text_sources(tmp_path)
    js_srcs = [s for s in srcs if "registry.js" in s.source_path]
    assert js_srcs, "displayName 是文本字段，脚本应产出文本源"
    vals = list(js_srcs[0].entries.values())
    assert "My Ingot" in vals          # 物品显示名 → 提取待翻译
    assert "my_ingot" not in vals      # 注册名（代码标识）→ 跳过
    # 行内替换：只改 displayName 的值，create 注册名与脚本结构原样
    out = render_pack_source(js_srcs[0], {k: "我的锭" for k in js_srcs[0].entries}, tmp_path)
    assert "event.create('my_ingot').displayName('我的锭')" in out


def test_pack_js_displayname_and_tooltip_extracted(tmp_path):
    """KubeJS 1.20.1 物品注册链式调用：displayName 驼峰 + tooltip 数组都提取，
    引用原版纹理等代码串（minecraft:item/...、tier、texture）跳过。"""
    from app.text_sources import discover_pack_text_sources, render_pack_source
    js = tmp_path / "kubejs" / "startup_scripts" / "test_items.js"
    js.parent.mkdir(parents=True)
    js.write_text(
        "StartupEvents.registry('item', event => {\n"
        "  event.create('zeno_sword', 'sword').displayName('Zeno Sword').tier('diamond')\n"
        "    .texture('minecraft:item/diamond_sword').maxStackSize(1)\n"
        "  event.create('obsidian_blade').displayName('Obsidian Blade')\n"
        "    .tooltip(['A sharp blade', 'Of obsidian'])\n"
        "})\n",
        encoding="utf-8")
    srcs = [s for s in discover_pack_text_sources(tmp_path)
            if "test_items.js" in s.source_path]
    assert srcs, "应发现 KubeJS 物品注册脚本文本源"
    vals = list(srcs[0].entries.values())
    assert "Zeno Sword" in vals and "Obsidian Blade" in vals
    assert "A sharp blade" in vals and "Of obsidian" in vals
    # 代码串 / 注册名 / 非文本字段不收集
    assert "zeno_sword" not in vals and "obsidian_blade" not in vals
    assert "diamond" not in vals                                  # tier 非文本字段
    assert "minecraft:item/diamond_sword" not in vals             # texture 有 / 拦截
    # 行内替换保留链式结构
    out = render_pack_source(srcs[0], {k: "中文" for k in srcs[0].entries}, tmp_path)
    assert ".displayName('中文')" in out and ".tier('diamond')" in out
    assert ".texture('minecraft:item/diamond_sword')" in out
    assert "tooltip(['中文', '中文'])" in out


def test_pack_snbt_multiline_description_array(tmp_path):
    """FTB description **跨行多元素数组**（quest_desc: [\n "a",\n "b"\n]）应被提取并翻译——
    修复 recheck：原逐行正则匹配不到跨行数组 → 整段任务描述漏翻译（用户实测长段 villager 说明没翻）。"""
    from app.text_sources import discover_pack_text_sources, render_pack_source
    pack = _make_pack(tmp_path, {
        "config/ftbquests/quests/lang/en_us.snbt": (
            'quest.00FD.quest_desc: [\n'
            '  "There are many changes to villager behavior present in Better Minecraft:",\n'
            '  "They will follow you if you have an Emerald Block in hand."\n'
            ']\n'
        ),
    })
    srcs = discover_pack_text_sources(pack)
    snbt = next(s for s in srcs if s.kind == "lines")
    vals = snbt.entries.values()
    assert "There are many changes to villager behavior present in Better Minecraft:" in vals
    assert "They will follow you if you have an Emerald Block in hand." in vals
    # 行内替换保留跨行结构（元素各自替换，不破坏 [ ] 与换行）
    out = render_pack_source(snbt, {k: "中文说明" for k in snbt.entries}, pack)
    assert 'quest_desc: [\n' in out
    assert '  "中文说明",\n' in out
    assert '  "中文说明"\n' in out
    assert ']' in out


def test_pack_snbt_desc_array_with_bracket_in_text(tmp_path):
    """description 数组元素含 ] 时不被提前截断（引号感知扫描跳过字符串内的 ]）。"""
    from app.text_sources import discover_pack_text_sources
    pack = _make_pack(tmp_path, {
        "config/ftbquests/quests/lang/en_us.snbt": (
            'quest.00FD.quest_desc: [\n'
            '  "Press [USE] to open",\n'
            '  "Second line"\n'
            ']\n'
        ),
    })
    srcs = discover_pack_text_sources(pack)
    snbt = next(s for s in srcs if s.kind == "lines")
    vals = snbt.entries.values()
    # [USE] 里的 ] 会让旧 [^\]]* 在第一个 ] 截断 → 后续元素漏
    assert "Press [USE] to open" in vals, "元素内 ] 不应截断数组"
    assert "Second line" in vals


def test_discover_skips_existing_target_lang_books(tmp_path):
    """mod 已自带 zh_cn 教程书（patchouli 书 zh_cn/ 目录）→ 跳过 en_us 源，不重复提取翻译
    （修复 recheck：本就适配中文的 mod 别胡扯重翻）。语言文件已由 diff 跳过，这里补 json/lines。"""
    from app.text_sources import discover_text_sources
    jar = tmp_path / "zhmod.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("assets/zhmod/patchouli_books/book/en_us/entries/start.json",
                    json.dumps({"text": "Hello World"}))
        zf.writestr("assets/zhmod/patchouli_books/book/zh_cn/entries/start.json",
                    json.dumps({"text": "你好世界"}))
        # 无 zh_cn 版本的控制组（另一本书）应正常提取
        zf.writestr("assets/zhmod/patchouli_books/book2/en_us/entries/start.json",
                    json.dumps({"text": "Second Book"}))
    srcs = discover_text_sources(jar, "zh_cn")
    js = [s for s in srcs if s.kind == "json"]
    assert not any("book/" in s.source_path for s in js), "自带 zh_cn 的书应跳过"
    assert any("book2/" in s.source_path for s in js), "无 zh_cn 的书应正常提取"


def test_pack_collects_zh_only_lang_placeholder():
    """v1.3.9 修复（用户「FTB/KubeJS 没全量翻译」）：只有 zh_cn 无 en_us 的 mod
    （ftbquestlocalizer 直接把英文写进 zh_cn 占位）——原只收 en_us 导致完全漏翻、
    产物原样英文。现在 assets/*/lang/ 全收，英文占位值收集翻译、已汉化值跳过。"""
    import tempfile
    from pathlib import Path
    from app.text_sources import _pack_json_source
    data = {
        "ftbquests.chapter.allthemodium.quest18F3B6750": "ATM Alloy tools",
        "ftbquests.chapter.allthemodium.quest31BDF73F7": "Allthemodium Intro",
        "key.already_zh": "钢铁合金工具",
    }
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        rel = "kubejs/assets/ftbquestlocalizer/lang/zh_cn.json"
        p = d / rel
        p.parent.mkdir(parents=True)
        p.write_text(__import__("json").dumps(data), encoding="utf-8")
        src = _pack_json_source(d, p, rel, "zh_cn")
        assert src is not None, "FTB 只有 zh_cn 英文占位的文件应被收集"
        assert src.target_path == rel, "target 应为原 zh_cn 文件（覆盖翻译）"
        assert "key.already_zh" not in src.entries, "已汉化值应跳过"
        assert len(src.entries) == 2, f"英文占位应收集 2 条，实际 {len(src.entries)}"
        assert src.entries["ftbquests.chapter.allthemodium.quest18F3B6750"] == "ATM Alloy tools"
