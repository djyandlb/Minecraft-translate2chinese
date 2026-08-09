# -*- coding: utf-8 -*-
"""M5-1 硬编码字节码扫描与替换核心的测试（真实 javac 编译验证）。"""

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from httpx import Response

import app.hardcode as hardcode
from app.hardcode import (
    ai_judge_translate,
    is_hardcode_translatable,
    replace_hardcoded_strings,
    scan_hardcoded_candidates,
    scan_hardcoded_strings,
)

JAVA_SRC = '''
public class HelloMod {
    public static void main(String[] args) {
        System.out.println("Hello World");
        System.out.println("Welcome to the server");
        System.out.println("iron_ingot");
        System.out.println("OK");
        System.out.println("mymod:item");
        System.out.println("com.example.Mod");
    }
}
'''


# 字节码级 Logger 剔除（P0-1 首选方案）：用「以 Logger 结尾的类 + info/error 方法」模拟
# log4j/slf4j 日志调用形态。日志串不是 failed/cannot 开头（正则不排除），
# 只能靠扫描层识别 ldc → 相邻 invoke Logger.x 剔除。
LOGGER_JAVA_SRC = '''
public class LogMod {
    static class Logger {
        static void info(String s) { }
        static void error(String s, Object... args) { }
    }
    public static void main(String[] args) {
        Logger.info("Config doesnt exist, creating new");
        Logger.error("A generic internal message", new Object[0]);
        System.out.println("Sky fog distance");
        System.out.println("Enable Voxy");
    }
}
'''


def _make_logger_jar(tmp_path: Path) -> Path:
    """javac 编译含 Logger 调用的类并打包成 jar。无 javac 则跳过测试。"""
    if shutil.which("javac") is None:
        pytest.skip("无 javac，跳过真实编译测试")
    srcdir = tmp_path / "src"
    srcdir.mkdir()
    (srcdir / "LogMod.java").write_text(LOGGER_JAVA_SRC, encoding="utf-8")
    classes = tmp_path / "classes"
    classes.mkdir()
    subprocess.run(
        ["javac", "-d", str(classes), str(srcdir / "LogMod.java")], check=True
    )
    jar = tmp_path / "logmod.jar"
    with zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in classes.rglob("*.class"):
            zf.write(f, f.relative_to(classes).as_posix())
    return jar


def test_scan_excludes_logger_strings(tmp_path):
    """字节码级 Logger 剔除：ldc 后相邻 invoke Logger.x 的字符串 → 不进候选。

    日志串不是正则形态（不以 failed/cannot 开头），证明是扫描层字节码识别起效，
    而不是 is_hardcode_translatable 的日志正则兜底。
    """
    jar = _make_logger_jar(tmp_path)
    found = scan_hardcoded_strings(jar)
    assert "Sky fog distance" in found
    assert "Enable Voxy" in found
    assert "Config doesnt exist, creating new" not in found
    assert "A generic internal message" not in found


def _make_test_jar(tmp_path: Path) -> Path:
    """javac 编译测试类并打包成 jar。无 javac 则跳过测试。"""
    if shutil.which("javac") is None:
        pytest.skip("无 javac，跳过真实编译测试")
    srcdir = tmp_path / "src"
    srcdir.mkdir()
    (srcdir / "HelloMod.java").write_text(JAVA_SRC, encoding="utf-8")
    classes = tmp_path / "classes"
    classes.mkdir()
    subprocess.run(
        ["javac", "-d", str(classes), str(srcdir / "HelloMod.java")], check=True
    )
    jar = tmp_path / "mod.jar"
    with zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in classes.rglob("*.class"):
            zf.write(f, f.relative_to(classes).as_posix())
    return jar


def test_is_hardcode_translatable():
    assert is_hardcode_translatable("Hello World")
    assert is_hardcode_translatable("欢迎来到服务器")
    assert is_hardcode_translatable("iron_ingot")           # snake_case 单词保留为候选（用户把关）
    assert not is_hardcode_translatable("OK")               # 纯大写缩写
    assert not is_hardcode_translatable("mymod:item")       # modid 前缀
    assert not is_hardcode_translatable("com.example.Mod")  # 类路径/包名
    assert not is_hardcode_translatable("12345")            # 纯数字
    # 粗过滤（voxy 实测：655 条硬编码候选绝大多数是技术串）：
    # 纯小写单词（≤16 字符、无空格）→ 技术标识符，排除
    assert not is_hardcode_translatable("stone")
    assert not is_hardcode_translatable("parent")
    assert not is_hardcode_translatable("model")
    assert not is_hardcode_translatable("voxy")
    assert not is_hardcode_translatable("id")
    assert not is_hardcode_translatable("path")
    assert not is_hardcode_translatable("minecraft")
    assert not is_hardcode_translatable("bobby")
    # 数据串/代码特征 → 排除
    assert not is_hardcode_translatable("position;aabb;x")             # 分号分隔数据串
    assert not is_hardcode_translatable("a|b")                         # 竖线分隔
    assert not is_hardcode_translatable("#version")                    # shader 指令
    assert not is_hardcode_translatable("printf")                      # 代码字面量
    assert not is_hardcode_translatable("textures/atlas/blocks.png")   # 资源路径
    assert not is_hardcode_translatable("{base_save_path}")            # 模板占位
    assert not is_hardcode_translatable("uint(")                       # 代码函数调用
    # 占位符保留：%s/%d 等（含空格完整句子不受格式串规则影响）
    assert is_hardcode_translatable("Size exceeds limits: %s")
    assert is_hardcode_translatable("Hello %s")
    # 纯格式串（printf 风格、无空格）→ 技术格式说明符，排除
    assert not is_hardcode_translatable("%.1f")
    assert not is_hardcode_translatable("%6.3f")
    assert not is_hardcode_translatable("%%CONST_ARRAY%%")
    # 纯技术串仍排除
    assert not is_hardcode_translatable("(Ljava/lang/String;)V")   # 方法签名
    assert not is_hardcode_translatable("HELLO_WORLD")             # 常量名
    assert not is_hardcode_translatable("1234567890abcdef")        # 十六进制
    assert not is_hardcode_translatable("true")                    # 字面量


def test_is_hardcode_log_exclusion():
    """P0-1 日志形态剔除：日志句式/堆栈标记 → False；GUI 文本保留。

    对照方块译匠「过滤前移」：扫描层就砍掉日志形态，不再留给 ai_judge。
    保守匹配（开头动词 + 技术名词），不误杀 GUI 错误提示。
    """
    logs = [
        "Failed to load config",
        "Failed to write config file",
        "Cannot create a child lower than lod level 0",
        "Cannot create a parent higher than LoD 4",
        "Could not parse config",
        "Unable to allocate geometry buffer, got gl error \x01",
        "Invalid block state in chunk stream",
        "Missing required dependency",
        "Exception in world cleaner",
        "Caused by: java.lang.NullPointerException",
        "not registered: some_mod:thing",
    ]
    for text in logs:
        assert not is_hardcode_translatable(text), text
    # GUI 可见文本必须保留（玩家看到的选项/提示）
    gui = [
        "Sky fog distance",
        "Enable Voxy",
        "Fog intensity",
        "Fog curve",
        "Cloud render distance in chunks",
        "Higher distance, sharper sky fog",
        "Multiplier for terrain fog opacity. 0.0 = off, 1.0 = vanilla",
    ]
    for text in gui:
        assert is_hardcode_translatable(text), text
    # 控制字符（\x01 等日志占位符）→ 排除（GUI 文本不含控制字符）
    assert not is_hardcode_translatable("Allocated new geometry buffer: \x01, isSparse: \x01")
    # 无空格 CamelCase → 方法名/类名等技术标识符排除（GUI 短语含空格；
    # 注意 snake_case 单词如 iron_ingot 仍保留为候选，交由 ai_judge 把关）
    assert not is_hardcode_translatable("verifyMeshing")
    assert not is_hardcode_translatable("ModelData")


def test_scan_hardcoded_strings(tmp_path):
    jar = _make_test_jar(tmp_path)
    found = scan_hardcoded_strings(jar)
    assert "Hello World" in found
    assert "Welcome to the server" in found
    assert "iron_ingot" in found            # MIT 规则：snake_case 单词保留为候选
    assert "OK" not in found
    assert "mymod:item" not in found
    assert "com.example.Mod" not in found


def test_scan_hardcoded_candidates_frequency(tmp_path):
    """候选扫描返回带出现频率的结构，且按频率降序。"""
    jar = _make_test_jar(tmp_path)
    cands = scan_hardcoded_candidates(jar)
    assert cands and all("occurrences" in c and "text" in c for c in cands)
    assert any(c["text"] == "Hello World" and c["occurrences"] >= 1 for c in cands)
    # 按出现频率降序排列（供前端候选列表按频率排序）
    freqs = [c["occurrences"] for c in cands]
    assert freqs == sorted(freqs, reverse=True)


def test_replace_hardcoded_strings(tmp_path):
    jar = _make_test_jar(tmp_path)
    mapping = {"Hello World": "你好世界", "Welcome to the server": "欢迎来到服务器"}
    result = replace_hardcoded_strings(jar, mapping)
    assert result["replaced"] == 2
    assert result["failed_classes"] == []
    # 重读验证：jar 内字符串已被替换
    found = scan_hardcoded_strings(jar)
    assert "你好世界" in found
    assert "欢迎来到服务器" in found
    assert "Hello World" not in found


# ---------- 嵌套包类用例（com.example 包路径斜杠规范化覆盖） ----------

NESTED_JAVA_SRC = '''
package com.example;

public class NestedMod {
    public static void main(String[] args) {
        System.out.println("Nested Hello");
        System.out.println("Nested Welcome");
    }
}
'''


def _make_nested_jar(tmp_path: Path) -> Path:
    """javac 编译带 package 声明的嵌套类并打包成 jar。无 javac 则跳过。"""
    if shutil.which("javac") is None:
        pytest.skip("无 javac，跳过真实编译测试")
    srcdir = tmp_path / "src" / "com" / "example"
    srcdir.mkdir(parents=True)
    (srcdir / "NestedMod.java").write_text(NESTED_JAVA_SRC, encoding="utf-8")
    classes = tmp_path / "classes"
    classes.mkdir()
    subprocess.run(
        ["javac", "-d", str(classes), str(srcdir / "NestedMod.java")], check=True
    )
    # 编译产物应在 com/example/NestedMod.class
    nested = classes / "com" / "example" / "NestedMod.class"
    assert nested.is_file(), "javac 未产出嵌套类 class 文件"
    jar = tmp_path / "nested.jar"
    with zipfile.ZipFile(jar, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in classes.rglob("*.class"):
            zf.write(f, f.relative_to(classes).as_posix())
    return jar


def test_scan_nested_package_class(tmp_path):
    jar = _make_nested_jar(tmp_path)
    found = scan_hardcoded_strings(jar)
    assert "Nested Hello" in found
    assert "Nested Welcome" in found


def test_replace_nested_package_class(tmp_path):
    jar = _make_nested_jar(tmp_path)
    mapping = {"Nested Hello": "嵌套你好", "Nested Welcome": "嵌套欢迎"}
    result = replace_hardcoded_strings(jar, mapping)
    assert result["replaced"] == 2
    assert result["failed_classes"] == []
    # 重读验证：嵌套类内字符串已被替换
    found = scan_hardcoded_strings(jar)
    assert "嵌套你好" in found
    assert "嵌套欢迎" in found
    assert "Nested Hello" not in found
    assert "Nested Welcome" not in found


# ---------- 损坏 class 容错（scan 不因单 class 崩溃） ----------

def test_scan_skip_bad_class(tmp_path):
    jar = _make_test_jar(tmp_path)
    evil = tmp_path / "bad.jar"
    with zipfile.ZipFile(evil, "w", zipfile.ZIP_DEFLATED) as zf:
        with zipfile.ZipFile(jar, "r") as src:
            for item in src.infolist():
                zf.writestr(item, src.read(item.filename))
        # 注入一个无法被 jawa 解析的损坏 class
        zf.writestr("Broken.class", "not a real class file")
    # 不应抛异常：坏 class 被跳过，正常 class 字符串仍能扫出
    found = scan_hardcoded_strings(evil)
    assert "Hello World" in found
    assert "Welcome to the server" in found


# ---------- zip-slip 路径穿越防护 ----------

def test_extract_jar_zip_slip(tmp_path):
    """恶意 jar 含 ../ 条目时不得逃逸出解压目录。"""
    jar = _make_test_jar(tmp_path)
    evil = tmp_path / "evil.jar"
    with zipfile.ZipFile(evil, "w", zipfile.ZIP_DEFLATED) as zf:
        with zipfile.ZipFile(jar, "r") as src:
            for item in src.infolist():
                zf.writestr(item, src.read(item.filename))
        zf.writestr("../escaped.txt", "PAYLOAD")
    # 扫描内部会解压：逃逸条目必须被跳过，不能落到 tmp_path
    scan_hardcoded_strings(evil)
    assert not (tmp_path / "escaped.txt").exists()
    # 替换同样不能逃逸
    replace_hardcoded_strings(evil, {"Hello World": "你好世界"})
    assert not (tmp_path / "escaped.txt").exists()


# ---------- replace 校验失败时原始字节还原 ----------

def test_replace_restores_bytes_on_verify_failure(tmp_path, monkeypatch):
    """校验失败（模拟重读抛异常）时 class 必须还原为原字节，不进输出 jar。"""
    jar = _make_test_jar(tmp_path)
    real_loader = hardcode._class_loader
    calls = {"n": 0}

    def flaky_loader(work):
        calls["n"] += 1
        if calls["n"] >= 2:  # 第二次调用即校验阶段：模拟重读失败
            raise RuntimeError("模拟校验失败")
        return real_loader(work)

    monkeypatch.setattr(hardcode, "_class_loader", flaky_loader)
    result = replace_hardcoded_strings(jar, {"Hello World": "你好世界"})
    monkeypatch.undo()
    # 失败 class 被记录，且替换数必须为 0（还原后无效替换）
    assert result["failed_classes"], "应记录校验失败的 class"
    assert result["replaced"] == 0
    # 输出 jar 重读：原字符串保留，被替换的字符串不得出现
    found = scan_hardcoded_strings(jar)
    assert "Hello World" in found
    assert "你好世界" not in found


# ---------- 内容级校验（M5-recheck：String 数量一致但内容被破坏时必须拦下） ----------

class _FakePool:
    """monkeypatch 用：伪常量池，pool.get() 返回自身，伪装成 String 引用的 UTF8 常量。"""

    def __init__(self, value: str):
        self.value = value

    def get(self, index):
        return self


def test_replace_content_mismatch_restores_bytes(tmp_path, monkeypatch):
    """内容级校验：重读后 String 数不变但内容与期望不符时，class 必须还原 + failed_classes + replaced 不虚高。

    模拟 jawa 的 Modified-UTF8 编码静默丢字符：期望写 "🎉 你好世界"，
    重读却得到丢 emoji 后的 " 你好世界"（String 数仍为 1，旧校验会漏报）。
    """
    from jawa.constants import String

    jar = _make_test_jar(tmp_path)
    real_loader = hardcode._class_loader
    calls = {"n": 0}

    def corrupting_loader(work):
        calls["n"] += 1
        if calls["n"] >= 2:  # 第二次调用即校验阶段：返回内容被"改坏"的伪 loader
            # 与 _make_test_jar 的 6 个 String 数量一致（数量校验拦不住），
            # 仅把映射命中的 "Hello World" 位置写成丢 emoji 后的 " 你好世界"
            corrupted = [
                " 你好世界", "Welcome to the server", "iron_ingot",
                "OK", "mymod:item", "com.example.Mod",
            ]

            class _FakeKlass:
                def __init__(self):
                    self.constants = [String(_FakePool(v), i, i)
                                      for i, v in enumerate(corrupted)]

            class _FakeLoader:
                def __getitem__(self, name):
                    return _FakeKlass()

            return _FakeLoader()
        return real_loader(work)

    monkeypatch.setattr(hardcode, "_class_loader", corrupting_loader)
    result = replace_hardcoded_strings(jar, {"Hello World": "🎉 你好世界"})
    monkeypatch.undo()
    # 内容不符必须被判定为失败：failed_classes 记录 + replaced 不虚高
    assert result["failed_classes"], "内容与期望不符应记入 failed_classes"
    assert result["replaced"] == 0
    # 输出 jar 重读：原字符串保留，损坏的译文不得出现
    found = scan_hardcoded_strings(jar)
    assert "Hello World" in found
    assert "🎉 你好世界" not in found


def test_replace_emoji_content_damage_restores(tmp_path):
    """内容级校验（真实场景）：含 emoji 的译文被 jawa Modified-UTF8 编码静默丢字符。

    jawa 的 encode_modified_utf8 对 U+10000+（emoji）无编码分支，静默丢弃；
    替换后 String 数不变但内容已损坏。旧校验只比数量会误判成功，
    内容级校验必须拦下：还原原字节 + failed_classes + replaced 不虚高。
    """
    jar = _make_test_jar(tmp_path)
    result = replace_hardcoded_strings(jar, {"Hello World": "🎉 你好世界"})
    assert result["failed_classes"], "emoji 内容损坏应被内容级校验拦下并记入 failed_classes"
    assert result["replaced"] == 0
    found = scan_hardcoded_strings(jar)
    assert "Hello World" in found          # 原字节已还原
    assert "🎉 你好世界" not in found       # 损坏内容不得进输出 jar


# ---------- B 阶段：scan 候选带相邻常量上下文（供 AI 判断） ----------

def test_scan_candidates_context(tmp_path):
    """scan_hardcoded_candidates 返回带 context 的候选；context 含同 class 的其他字符串常量。"""
    jar = _make_test_jar(tmp_path)
    cands = scan_hardcoded_candidates(jar)
    hw = next(c for c in cands if c["text"] == "Hello World")
    assert "context" in hw and isinstance(hw["context"], list)
    # context 含同 class 的其他 String 常量（排除自身）
    assert "Welcome to the server" in hw["context"]
    # context 去重截断：≤30 条、每条 ≤80 字符
    assert len(hw["context"]) <= 30
    assert all(len(s) <= 80 for s in hw["context"])
    # 其余候选同样带 context 字段
    assert all("context" in c for c in cands)


# ---------- B 阶段：ai_judge_translate（LLM 判断 + 翻译） ----------

def _llm_engine_with(handler) -> "LLMClient":
    """构造注入 MockTransport 的 LLMClient（ai_judge 测试用，不走真实网络）。"""
    from httpx import AsyncClient, MockTransport
    from app.translate.llm import LLMClient

    engine = LLMClient("https://x", "k", "m", concurrency=2, batch_size=10)
    engine._client = AsyncClient(transport=MockTransport(handler))
    return engine


# ---------- P0-2：ai_judge 三分类（translate / exclude / unresolved） ----------

@pytest.mark.asyncio
async def test_ai_judge_translate():
    """LLM 判断：action=translate 的进 translations，action=exclude 的进 excluded。"""
    def handler(request):
        return Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"decisions": [
                {"text": "Hello World", "action": "translate", "translation": "你好世界"},
                {"text": "stone", "action": "exclude", "reason": "not_user_visible"},
            ]})}}]})

    engine = _llm_engine_with(handler)
    candidates = [
        {"text": "Hello World", "context": ["Welcome to the server"]},
        {"text": "stone", "context": ["iron_ingot"]},
    ]
    result = await ai_judge_translate(engine, candidates, "zh_cn")
    assert result.translations == {"Hello World": "你好世界"}
    assert result.excluded == ["stone"]
    assert result.unresolved == []
    await engine._client.aclose()


@pytest.mark.asyncio
async def test_ai_judge_exclude_developer_log_reason():
    """P0-2：LLM 明确以 developer_log 排除的日志 → 进 excluded，不翻译。"""
    def handler(request):
        return Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"decisions": [
                {"text": "Sky fog distance", "action": "translate", "translation": "天空雾距离"},
                {"text": "Failed to load config", "action": "exclude", "reason": "developer_log"},
            ]})}}]})

    engine = _llm_engine_with(handler)
    result = await ai_judge_translate(engine, [
        {"text": "Sky fog distance", "context": []},
        {"text": "Failed to load config", "context": []},
    ], "zh_cn")
    assert result.translations == {"Sky fog distance": "天空雾距离"}
    assert result.excluded == ["Failed to load config"]
    assert result.unresolved == []
    await engine._client.aclose()


@pytest.mark.asyncio
async def test_ai_judge_unresolved_retry_single():
    """P0-2：LLM 批量只返回部分候选 → 未返回的进 unresolved 并单独重试，重试成功并入。"""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        payload = json.loads(request.content)
        batch = json.loads(payload["messages"][-1]["content"])
        # 无论批量还是单条重试，都只返回第一个候选的 translate
        return Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"decisions": [
                {"text": batch[0]["text"], "action": "translate", "translation": "译" + batch[0]["text"]}
            ]})}}]})

    engine = _llm_engine_with(handler)
    result = await ai_judge_translate(engine, [
        {"text": "A", "context": []},
        {"text": "B", "context": []},
    ], "zh_cn")
    # 批量只处置 A → B unresolved → 单独重试 B 成功
    assert result.translations == {"A": "译A", "B": "译B"}
    assert result.unresolved == []
    assert calls["n"] == 2   # 1 批量 + 1 单条重试
    await engine._client.aclose()


@pytest.mark.asyncio
async def test_ai_judge_unresolved_kept_after_retry():
    """P0-2：unresolved 单条重试最多 2 次仍未解决 → 保留在 unresolved，不静默漏判。"""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"decisions": [
                {"text": "other", "action": "translate", "translation": "x"}
            ]})}}]})

    engine = _llm_engine_with(handler)
    result = await ai_judge_translate(engine, [{"text": "Ghost", "context": []}], "zh_cn")
    assert result.translations == {}
    assert result.unresolved == ["Ghost"]
    assert calls["n"] == 3   # 1 批量 + 2 次单条重试
    await engine._client.aclose()


@pytest.mark.asyncio
async def test_ai_judge_translate_invalid_json_unresolved():
    """LLM 输出非法 JSON → 逐条降级失败 → 进 unresolved（不整批静默丢）。"""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return Response(200, json={"choices": [{"message": {"content": "这不是 JSON"}}]})

    engine = _llm_engine_with(handler)
    result = await ai_judge_translate(engine, [{"text": "Hello", "context": []}], "zh_cn")
    assert result.translations == {}
    assert result.unresolved == ["Hello"]
    assert calls["n"] == 4   # 1 批量 + 1 逐条降级 + 2 次 unresolved 重试
    await engine._client.aclose()


@pytest.mark.asyncio
async def test_ai_judge_translate_missing_field_unresolved():
    """LLM 输出缺失 action/translation 字段 → 该条进 unresolved，不崩。"""
    def handler(request):
        return Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"decisions": [
                {"text": "Hello World"},                      # 缺 action/translation
                {"text": "Good day", "action": "translate"},  # 缺 translation
            ]})}}]})

    engine = _llm_engine_with(handler)
    result = await ai_judge_translate(engine, [
        {"text": "Hello World", "context": []},
        {"text": "Good day", "context": []},
    ], "zh_cn")
    assert result.translations == {}
    assert set(result.unresolved) == {"Hello World", "Good day"}
    await engine._client.aclose()


@pytest.mark.asyncio
async def test_ai_judge_translate_paging_concurrent():
    """分页并发：候选超过 25 条时分多批并发请求，每批 ≤25 条。

    Semaphore 限流并发 + asyncio.gather（对齐 LLMClient.translate_batch 的并发模式）。
    async handler 让出事件循环以放大并发窗口，断言同一时刻有多个请求 in-flight。
    """
    import asyncio

    seen_sizes = []
    active = 0
    max_active = 0

    async def handler(request):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)   # 让出事件循环：放大并发窗口
        payload = json.loads(request.content)
        batch = json.loads(payload["messages"][-1]["content"])
        seen_sizes.append(len(batch))
        active -= 1
        return Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"decisions": [
                {"text": item["text"], "action": "translate", "translation": "译"}
                for item in batch
            ]})}}]})

    engine = _llm_engine_with(handler)
    candidates = [{"text": f"t{i}", "context": []} for i in range(60)]
    result = await ai_judge_translate(engine, candidates, "zh_cn")
    assert len(result.translations) == 60
    assert result.unresolved == []
    assert sorted(seen_sizes) == [10, 25, 25]   # 60 → 25 + 25 + 10（并发下顺序不定）
    assert max_active >= 2                      # 至少两个批次同时 in-flight
    await engine._client.aclose()


@pytest.mark.asyncio
async def test_ai_judge_translate_null_content_unresolved():
    """LLM 返回 content=null → 该批全进 unresolved，不抛 AttributeError（B 审查 🟡1）。"""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return Response(200, json={"choices": [{"message": {"content": None}}]})

    engine = _llm_engine_with(handler)
    result = await ai_judge_translate(engine, [{"text": "Hello", "context": []}], "zh_cn")
    assert result.translations == {}
    assert result.unresolved == ["Hello"]
    assert calls["n"] == 3   # 1 批量 + 2 次单条重试
    await engine._client.aclose()


@pytest.mark.asyncio
async def test_ai_judge_system_prompt_uses_target_lang():
    """target_lang 注入 system prompt：zh_tw → 提示繁体中文，不写死简体（B 审查 🟡2）。"""
    seen = {}

    def handler(request):
        payload = json.loads(request.content)
        seen["sys"] = payload["messages"][0]["content"]
        return Response(200, json={"choices": [{"message": {"content": "[]"}}]})

    engine = _llm_engine_with(handler)
    await ai_judge_translate(engine, [{"text": "Hello", "context": []}], "zh_tw")
    assert "zh_tw" in seen["sys"]
    assert "繁体中文" in seen["sys"]
    await engine._client.aclose()


def test_ai_judge_system_prompt_mix_rules():
    """P0-2/P0-3：ai_judge system prompt 含日志排除边界 + 混排约束 + 严格 JSON。"""
    import app.hardcode as hardcode
    s = hardcode._ai_judge_system_prompt("zh_cn")
    assert "玩家在游戏中能直接看到" in s
    # 日志/技术串明确排除（照方块译匠）
    assert "日志" in s and "排除" in s
    assert "developer_log" in s
    # 混排约束
    assert "中英" in s and ("混杂" in s or "混排" in s or "硬插" in s)
    # 严格 JSON 结构
    assert "decisions" in s and "action" in s


@pytest.mark.asyncio
async def test_ai_judge_known_translations_injected():
    """P0-3：已确认术语注入 ai_judge user prompt（强制沿用已确认译名）。"""
    seen = {}

    def handler(request):
        payload = json.loads(request.content)
        seen["user"] = payload["messages"][-1]["content"]
        return Response(200, json={"choices": [{"message": {"content": "[]"}}]})

    engine = _llm_engine_with(handler)
    await ai_judge_translate(engine, [{"text": "Sky fog distance", "context": []}], "zh_cn",
                             known_translations={"Sky fog distance": "天空雾距离"})
    assert "Sky fog distance" in seen["user"]
    assert "天空雾距离" in seen["user"]
    assert "已确认" in seen["user"]
    await engine._client.aclose()


# ---------- P0 止血：ai_judge 非法 JSON 逐条降级（不整批丢） ----------

@pytest.mark.asyncio
async def test_ai_judge_invalid_json_downgrades_per_item():
    """ai_judge 整批非法 JSON → 对该批候选逐条降级，不整批丢（P0 根因 3）。"""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return Response(200, json={"choices": [{"message": {"content": "这不是 JSON"}}]})
        # 逐条降级：单候选返回合法 JSON
        payload = json.loads(request.content)
        batch = json.loads(payload["messages"][-1]["content"])
        return Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"decisions": [
                {"text": b["text"], "action": "translate", "translation": "译" + b["text"]}
                for b in batch
            ]})}}]})

    engine = _llm_engine_with(handler)
    result = await ai_judge_translate(engine, [
        {"text": "Hello World", "context": []},
        {"text": "Good day", "context": []},
    ], "zh_cn")
    assert result.translations == {"Hello World": "译Hello World", "Good day": "译Good day"}
    assert result.unresolved == []
    assert calls["n"] == 3   # 1 批量 + 2 逐条降级
    await engine._client.aclose()


@pytest.mark.asyncio
async def test_ai_judge_empty_array_downgrades():
    """ai_judge 输出空数组 [] → 逐条降级，不静默丢（P0 根因 3）。"""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return Response(200, json={"choices": [{"message": {"content": "[]"}}]})
        payload = json.loads(request.content)
        batch = json.loads(payload["messages"][-1]["content"])
        return Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"decisions": [
                {"text": b["text"], "action": "translate", "translation": "译" + b["text"]}
                for b in batch
            ]})}}]})

    engine = _llm_engine_with(handler)
    result = await ai_judge_translate(engine, [{"text": "Hello", "context": []}], "zh_cn")
    assert result.translations == {"Hello": "译Hello"}
    assert result.unresolved == []
    assert calls["n"] == 2   # 1 批量 + 1 逐条降级
    await engine._client.aclose()
