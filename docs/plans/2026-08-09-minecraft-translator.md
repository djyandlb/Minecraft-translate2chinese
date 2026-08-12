# Minecraft 整合包自动翻译工具 实施计划

> **给代理工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实施此计划。步骤使用复选框（`- [ ]`）语法进行跟踪。

**目标：** 构建 Windows 桌面应用「像素译站」——把整合包、单个 mod、地图里的英文文本自动翻译成中文（目标语言参数化，默认 `zh_cn`），核心是「扫描提取 → 翻译引擎 → 资源包/改写输出」流水线。

**架构：** Python FastAPI 后端 + Vue3 前端。开发期散装多文件直接跑（uvicorn + vite dev，浏览器访问），**不打包**；最后 debug 完才进入 M6 打包（pywebview 加载本地静态资源 + PyInstaller + Inno Setup 出安装版，另出单文件便携版，共两个版本）。翻译引擎抽象为可插拔接口，UI 互斥选择「LLM API」或「在线机翻」。

**技术栈：** Python 3.11+、FastAPI、uvicorn、httpx、deep-translator、pytest；前端 Vue3 + Vite + Tailwind；地图 nbtlib；打包 pywebview + PyInstaller + Inno Setup。

## 全局约束

- Python ≥ 3.11；全部用 `pathlib` 处理路径，禁止手拼路径字符串
- **开发期不打包**：后端 `cd backend && uvicorn app.main:app --reload`，前端 `npm run dev`，浏览器访问；打包只在 M6
- 打包两个版本：① Inno Setup 安装版 ② PyInstaller 单文件便携版；桌面壳 pywebview
- 目标语言参数化：模块只认 `target_lang`（默认 `zh_cn`）与 `source_lang`（默认 `en_us`），不写死中文
- 引擎互斥：`config.engine ∈ {"llm","machine"}`，选哪个用哪个，禁止自动降级混用；所选引擎失败时 UI 提示用户切换
- 占位符/格式码必须保护：`%s`、`%1$s`、`{var}`、`<item:...>`、`{{...}}`、`§[0-9a-fk-or]`、`\n`
- 复用许可铁律（见 `docs/UPSTREAM.md`）：MCC-i18n（MIT）可直接拷贝保留声明；mc_translator（MIT，Rust）转译思想；xtmc（无 LICENSE）只参考算法全自研；zVictorium（CC BY-NC）非商用借鉴并署名
- 代码注释、文档、UI 文本、提交信息全部用中文
- 每个任务结束必须 `git commit`

---

## 文件结构

```
Minecraft-translate/
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py              # FastAPI 入口（任务 13）
│   │   ├── models.py            # Pydantic 请求/响应模型（任务 13）
│   │   ├── config.py            # AppConfig（任务 1）
│   │   ├── langfile.py          # 语言文件解析/写出（任务 2）
│   │   ├── version.py           # MC 版本→pack_format→语言格式适配（任务 7）
│   │   ├── memory.py            # 翻译记忆持久化（任务 12）
│   │   ├── jar.py               # jar 枚举/解压/重打包（任务 3）
│   │   ├── scanner.py           # 整合包目录与单 jar 扫描（任务 4）
│   │   ├── diff.py              # 差集/翻译缺口计算（任务 5）
│   │   ├── placeholder.py       # 占位符保护/恢复（任务 6）
│   │   ├── resourcepack.py      # 资源包生成（任务 7）
│   │   ├── translate/
│   │   │   ├── __init__.py
│   │   │   ├── engine.py        # 引擎抽象 + 工厂（任务 8）
│   │   │   ├── common.py        # 跳过滤规则（任务 8）
│   │   │   ├── llm.py           # OpenAI 兼容 LLM 客户端（任务 9）
│   │   │   └── machine.py       # 在线机翻（任务 10）
│   │   ├── glossary.py          # 术语库（任务 11）
│   │   ├── archive.py           # 整合包压缩包解压 zip/mrpack（V3）
│   │   ├── translate/
│   │   │   ├── __init__.py
│   │   │   ├── engine.py        # 引擎抽象 + 工厂（任务 8）
│   │   │   ├── common.py        # 跳过滤规则（任务 8）
│   │   │   ├── providers.py     # LLM 厂商预置模板与智能默认（V3）
│   │   │   ├── llm.py           # OpenAI 兼容 LLM 客户端（任务 9）
│   │   │   ├── machine.py       # 在线机翻（任务 10）
│   │   │   └── han.py           # 简繁直转 OpenCC（V3）
│   │   └── tasks.py             # 任务状态/断点存储（任务 12）
│   ├── tests/
│   │   ├── fixtures/            # 样例 jar/json/lang
│   │   └── test_*.py
│   └── requirements.txt
├── frontend/                    # Vue3 + Vite + Tailwind（任务 14）
├── docs/
│   ├── plans/2026-08-09-minecraft-translator.md
│   └── UPSTREAM.md
├── scripts/build_installer.ps1  # M6 打包脚本
├── _upstream/                   # 参考仓库（gitignore）
├── .gitignore
└── README.md
```

---

## M0 准备

### 任务 1：项目骨架与配置模块

**文件：**
- 创建：`pyproject.toml`、`backend/requirements.txt`、`.gitignore`、`README.md`
- 创建：`backend/app/__init__.py`、`backend/app/config.py`
- 测试：`backend/tests/__init__.py`、`backend/tests/test_config.py`

**接口：**
- 产生：`class AppConfig` — `__init__(self, path: Path)`、`save(self) -> None`、`get(self, key: str, default=None)`、`set(self, key: str, value) -> None`

- [ ] **步骤 1：初始化仓库与目录**

```bash
cd "E:/claude code/Minecraft-translate" && git init && mkdir -p backend/app backend/tests frontend docs/plans scripts
```

- [ ] **步骤 2：写依赖与忽略文件**

`backend/requirements.txt`：
```
fastapi==0.115.*
uvicorn[standard]==0.32.*
httpx==0.28.*
python-multipart==0.0.20
deep-translator==1.11.4
nbtlib==2.0.4
pytest==8.3.*
pytest-asyncio==0.24.*
keyring==25.*
```

`.gitignore`：
```
__pycache__/
*.pyc
.venv/
node_modules/
dist/
_upstream/
*.log
```

- [ ] **步骤 3：编写失败测试**

`backend/tests/test_config.py`：
```python
from pathlib import Path
from app.config import AppConfig

def test_defaults(tmp_path: Path):
    cfg = AppConfig(tmp_path / "cfg.json")
    assert cfg.get("engine") == "llm"
    assert cfg.get("target_lang") == "zh_cn"

def test_save_and_reload(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = AppConfig(p)
    cfg.set("target_lang", "zh_tw")
    cfg.save()
    assert AppConfig(p).get("target_lang") == "zh_tw"
```

- [ ] **步骤 4：运行确认失败**

运行：`cd backend && python -m pytest tests/test_config.py -v`
预期：FAIL，`ModuleNotFoundError: No module named 'app'`

- [ ] **步骤 5：实现配置模块**

`backend/app/config.py`：
```python
import json
from pathlib import Path

DEFAULT_CONFIG = {
    "engine": "llm",                       # "llm" | "machine"，互斥
    "source_lang": "en_us",
    "target_lang": "zh_cn",
    "llm": {"base_url": "", "api_key": "", "model": "deepseek-chat"},
    "machine": {"provider": "google"},
    "concurrency": 5,
    "batch_size": 50,
    "pack_format": 15,
}

class AppConfig:
    """应用配置：json 文件读写，点号键 get/set。"""
    def __init__(self, path: Path):
        self.path = path
        self.data = dict(DEFAULT_CONFIG)
        if path.exists():
            self.data.update(json.loads(path.read_text(encoding="utf-8")))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value
```

- [ ] **步骤 6：运行确认通过**

运行：`cd backend && python -m pytest tests/test_config.py -v`
预期：PASS（2 passed）

- [ ] **步骤 7：提交**

```bash
git add -A && git commit -m "feat: 项目骨架与配置模块"
```

---

## M1 语言文件核心

### 任务 2：语言文件解析器

**文件：**
- 创建：`backend/app/langfile.py`
- 测试：`backend/tests/test_langfile.py`、`backend/tests/fixtures/sample.lang`、`backend/tests/fixtures/sample.json`

**接口：**
- 消费：无
- 产生：`parse_json_lang(text: str) -> dict[str, str]`、`parse_lang(text: str) -> dict[str, str]`、`load_lang_file(path: Path) -> tuple[dict[str, str], str]`、`write_json_lang(data: dict[str, str]) -> str`、`write_lang(data: dict[str, str]) -> str`

- [ ] **步骤 1：编写失败测试**

`backend/tests/test_langfile.py`：
```python
from pathlib import Path
from app.langfile import parse_lang, parse_json_lang, load_lang_file, write_json_lang, write_lang

def test_parse_lang_ignores_comments():
    text = "# 注释\nitem.iron=铁锭\nitem.gold = 金锭\n"
    assert parse_lang(text) == {"item.iron": "铁锭", "item.gold": "金锭"}

def test_parse_json_with_comments():
    text = '{\n  // 注释\n  "item.iron": "铁锭",\n  "item.gold": "金锭"\n}'
    assert parse_json_lang(text) == {"item.iron": "铁锭", "item.gold": "金锭"}

def test_load_and_write_roundtrip(tmp_path: Path):
    p = tmp_path / "a.json"
    p.write_text('{"k": "值"}', encoding="utf-8")
    entries, fmt = load_lang_file(p)
    assert fmt == "json" and entries == {"k": "值"}
    # 写出后能重新读回，验证往返一致
    out = p.with_name("out.json")
    out.write_text(write_json_lang(entries), encoding="utf-8")
    assert load_lang_file(out)[0] == {"k": "值"}
```

- [ ] **步骤 2：运行确认失败**

运行：`cd backend && python -m pytest tests/test_langfile.py -v`
预期：FAIL，`ModuleNotFoundError`

- [ ] **步骤 3：实现**

`backend/app/langfile.py`：
```python
import json
import re
from pathlib import Path

_COMMENT_RE = re.compile(r"//.*?$|/\*.*?\*/", re.MULTILINE | re.DOTALL)

def parse_json_lang(text: str) -> dict[str, str]:
    """解析 JSON 语言文件，容忍 // 与 /* */ 注释（部分 mod 会写）。"""
    cleaned = _COMMENT_RE.sub("", text)
    data = json.loads(cleaned)
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}

def parse_lang(text: str) -> dict[str, str]:
    """解析 .lang 语言文件：每行 key=value，# 开头为注释。"""
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        result[k.strip()] = v.strip()
    return result

def load_lang_file(path: Path) -> tuple[dict[str, str], str]:
    """读语言文件，返回 (entries, 格式)，格式为 "json" 或 "lang"。"""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return parse_json_lang(text), "json"
    return parse_lang(text), "lang"

def write_json_lang(data: dict[str, str]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)

def write_lang(data: dict[str, str]) -> str:
    return "\n".join(f"{k}={v}" for k, v in data.items()) + "\n"
```

- [ ] **步骤 4：运行确认通过**

运行：`cd backend && python -m pytest tests/test_langfile.py -v`
预期：PASS

- [ ] **步骤 5：提交**

```bash
git add -A && git commit -m "feat: 语言文件解析器 json/lang"
```

### 任务 3：jar 处理

**文件：**
- 创建：`backend/app/jar.py`
- 测试：`backend/tests/test_jar.py`

**接口：**
- 消费：`app.langfile.load_lang_file`（可后续）
- 产生：`list_jar_lang_files(jar_path: Path) -> list[dict]`（元素 `{"path","modid","lang","format"}`）、`extract_jar_to(jar_path: Path, out_dir: Path) -> None`、`pack_dir_to_jar(src_dir: Path, jar_path: Path) -> None`

- [ ] **步骤 1：编写失败测试**

`backend/tests/test_jar.py`：
```python
import json, zipfile
from pathlib import Path
from app.jar import list_jar_lang_files, extract_jar_to, pack_dir_to_jar

def _make_jar(path: Path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/mods.toml", "modId=\"demo\"\n")
        zf.writestr("assets/demo/lang/en_us.json", json.dumps({"item.x": "Iron"}))
        zf.writestr("assets/demo/lang/zh_cn.json", json.dumps({"item.x": "铁"}))

def test_list_lang_files(tmp_path: Path):
    jar = tmp_path / "demo.jar"
    _make_jar(jar)
    files = list_jar_lang_files(jar)
    langs = {f["lang"] for f in files}
    assert langs == {"en_us", "zh_cn"}
    assert all(f["modid"] == "demo" for f in files)

def test_pack_roundtrip(tmp_path: Path):
    jar = tmp_path / "a.jar"
    _make_jar(jar)
    out = tmp_path / "out"
    extract_jar_to(jar, out)
    repacked = tmp_path / "b.jar"
    pack_dir_to_jar(out, repacked)
    assert set(list_jar_lang_files(repacked)) == set(list_jar_lang_files(jar))
```

- [ ] **步骤 2：运行确认失败**

运行：`cd backend && python -m pytest tests/test_jar.py -v`
预期：FAIL

- [ ] **步骤 3：实现**

`backend/app/jar.py`：
```python
import re
import zipfile
from pathlib import Path

_LANG_RE = re.compile(r"^assets/([^/]+)/lang/([a-z0-9_]+)\.(json|lang)$")

def list_jar_lang_files(jar_path: Path) -> list[dict]:
    """枚举 jar 内所有语言文件条目。"""
    result: list[dict] = []
    with zipfile.ZipFile(jar_path) as zf:
        for name in zf.namelist():
            m = _LANG_RE.match(name)
            if m:
                result.append({
                    "path": name,
                    "modid": m.group(1),
                    "lang": m.group(2),
                    "format": m.group(3),
                })
    return result

def extract_jar_to(jar_path: Path, out_dir: Path) -> None:
    """解压 jar 到 out_dir。"""
    with zipfile.ZipFile(jar_path) as zf:
        zf.extractall(out_dir)

def pack_dir_to_jar(src_dir: Path, jar_path: Path) -> None:
    """把目录重新打成 jar。"""
    with zipfile.ZipFile(jar_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(src_dir.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(src_dir).as_posix())
```

- [ ] **步骤 4：运行确认通过**

运行：`cd backend && python -m pytest tests/test_jar.py -v`
预期：PASS

- [ ] **步骤 5：提交**

```bash
git add -A && git commit -m "feat: jar 语言文件枚举与解压重打包"
```

### 任务 4：扫描器（整合包目录 / 单 jar）

**文件：**
- 创建：`backend/app/scanner.py`
- 测试：`backend/tests/test_scanner.py`

**接口：**
- 消费：`app.jar.list_jar_lang_files`、`app.langfile.load_lang_file`
- 产生：`@dataclass ModScan`（字段 `jar_path: Path`、`modid: str`、`source_entries: dict[str, str]`、`target_entries: dict[str, str]`、`lang_format: str`）、`scan_modpack(dir: Path, source_lang: str, target_lang: str) -> list[ModScan]`、`scan_jar(jar_path: Path, source_lang: str, target_lang: str) -> list[ModScan]`

- [ ] **步骤 1：编写失败测试**

`backend/tests/test_scanner.py`：
```python
import json, zipfile
from pathlib import Path
from app.scanner import scan_modpack, scan_jar

def _jar(path: Path, modid: str, en: dict, zh: dict):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"assets/{modid}/lang/en_us.json", json.dumps(en))
        zf.writestr(f"assets/{modid}/lang/zh_cn.json", json.dumps(zh))

def test_scan_jar_gaps(tmp_path: Path):
    jar = tmp_path / "demo.jar"
    _jar(jar, "demo", {"a": "One", "b": "Two"}, {"a": "一"})
    scans = scan_jar(jar, "en_us", "zh_cn")
    assert len(scans) == 1
    assert "b" in scans[0].source_entries and "b" not in scans[0].target_entries

def test_scan_modpack_collects_jars(tmp_path: Path):
    mods = tmp_path / "mods"
    mods.mkdir()
    _jar(mods / "m1.jar", "m1", {"k": "v"}, {})
    scans = scan_modpack(tmp_path, "en_us", "zh_cn")
    assert [s.modid for s in scans] == ["m1"]
```

- [ ] **步骤 2：运行确认失败** → 预期 FAIL（模块不存在）

- [ ] **步骤 3：实现**

`backend/app/scanner.py`：
```python
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from app.jar import list_jar_lang_files

@dataclass
class ModScan:
    jar_path: Path
    modid: str
    source_entries: dict[str, str]
    target_entries: dict[str, str] = field(default_factory=dict)
    lang_format: str = "json"

def _read_entries(zf: zipfile.ZipFile, path: str) -> dict[str, str]:
    raw = json.loads(zf.read(path).decode("utf-8"))
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}

def _scan_one_jar(jar: Path, source_lang: str, target_lang: str) -> list[ModScan]:
    """解析单个 jar 内所有 modid 的语言文件，返回扫描结果（一 jar 可能含多 modid）。"""
    results: list[ModScan] = []
    with zipfile.ZipFile(jar) as zf:
        for info in list_jar_lang_files(jar):
            if info["lang"] != source_lang:
                continue
            tgt_path = f"assets/{info['modid']}/lang/{target_lang}.{info['format']}"
            src = _read_entries(zf, info["path"])
            tgt = _read_entries(zf, tgt_path) if tgt_path in zf.namelist() else {}
            results.append(ModScan(jar_path=jar, modid=info["modid"],
                                   source_entries=src, target_entries=tgt,
                                   lang_format=info["format"]))
    return results

def scan_jar(jar_path: Path, source_lang: str, target_lang: str) -> list[ModScan]:
    return _scan_one_jar(jar_path, source_lang, target_lang)

def scan_modpack(dir: Path, source_lang: str, target_lang: str, scope: str = "mods") -> list[ModScan]:
    """扫描整合包目录。scope="mods" 仅扫 mods/**/*.jar（默认，避免误扫资源包/存档）；
    scope="all" 时全目录递归。"""
    results: list[ModScan] = []
    root = dir / "mods" if scope == "mods" else dir
    if not root.exists():
        return results
    for jar in sorted(root.rglob("*.jar")):
        results.extend(_scan_one_jar(jar, source_lang, target_lang))
    return results
```python
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from app.jar import list_jar_lang_files

@dataclass
class ModScan:
    jar_path: Path
    modid: str
    source_entries: dict[str, str]
    target_entries: dict[str, str] = field(default_factory=dict)
    lang_format: str = "json"

def _read_entries(zf: zipfile.ZipFile, path: str) -> dict[str, str]:
    raw = json.loads(zf.read(path).decode("utf-8"))
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}

def _scan_one_jar(jar: Path, source_lang: str, target_lang: str) -> list[ModScan]:
    results: list[ModScan] = []
    with zipfile.ZipFile(jar) as zf:
        for info in list_jar_lang_files(jar):
            if info["lang"] != source_lang:
                continue
            tgt_path = f"assets/{info['modid']}/lang/{target_lang}.{info['format']}"
            src = _read_entries(zf, info["path"])
            tgt = _read_entries(zf, tgt_path) if tgt_path in zf.namelist() else {}
            results.append(ModScan(jar_path=jar, modid=info["modid"],
                                   source_entries=src, target_entries=tgt,
                                   lang_format=info["format"]))
    return results

def scan_jar(jar_path: Path, source_lang: str, target_lang: str) -> list[ModScan]:
    return _scan_one_jar(jar_path, source_lang, target_lang)

def scan_modpack(dir: Path, source_lang: str, target_lang: str) -> list[ModScan]:
    results: list[ModScan] = []
    for jar in sorted(dir.rglob("*.jar")):
        results.extend(_scan_one_jar(jar, source_lang, target_lang))
    return results
```

- [ ] **步骤 4：运行确认通过**

运行：`cd backend && python -m pytest tests/test_scanner.py -v`
预期：PASS

- [ ] **步骤 5：提交**

```bash
git add -A && git commit -m "feat: 整合包与单 jar 扫描器"
```

### 任务 5：差集与翻译缺口

**文件：**
- 创建：`backend/app/diff.py`
- 测试：`backend/tests/test_diff.py`

**接口：**
- 消费：`app.scanner.ModScan`
- 产生：`compute_gaps(source: dict[str, str], existing: dict[str, str]) -> list[str]`、`@dataclass TranslationJob`（字段 `modid: str`、`key: str`、`source_text: str`）、`build_jobs(scans: list[ModScan]) -> list[TranslationJob]`

- [ ] **步骤 1：编写失败测试**

`backend/tests/test_diff.py`：
```python
from app.diff import compute_gaps, build_jobs
from app.scanner import ModScan

def test_gaps_skip_present_and_empty():
    src = {"a": "One", "b": "Two", "c": "Three"}
    existing = {"a": "一", "b": ""}
    assert compute_gaps(src, existing) == ["c"]

def test_build_jobs_aggregates_mods():
    scans = [
        ModScan(jar_path=None, modid="m1", source_entries={"x": "Hi"}, target_entries={}),
        ModScan(jar_path=None, modid="m2", source_entries={"y": "Bye"}, target_entries={"y": "再见"}),
    ]
    jobs = build_jobs(scans)
    assert [(j.modid, j.key, j.source_text) for j in jobs] == [("m1", "x", "Hi")]
```

- [ ] **步骤 2：运行确认失败** → 预期 FAIL

- [ ] **步骤 3：实现**

`backend/app/diff.py`：
```python
from dataclasses import dataclass
from app.scanner import ModScan

def compute_gaps(source: dict[str, str], existing: dict[str, str]) -> list[str]:
    """返回 source 中缺失或值为空的 key（已有翻译的不翻）。"""
    return [k for k in source if k not in existing or not existing[k].strip()]

@dataclass
class TranslationJob:
    modid: str
    key: str
    source_text: str

def build_jobs(scans: list[ModScan]) -> list[TranslationJob]:
    """把所有 mod 的翻译缺口汇总成作业列表。"""
    jobs: list[TranslationJob] = []
    for scan in scans:
        for key in compute_gaps(scan.source_entries, scan.target_entries):
            jobs.append(TranslationJob(scan.modid, key, scan.source_entries[key]))
    return jobs
```

- [ ] **步骤 4：运行确认通过** → 预期 PASS

- [ ] **步骤 5：提交**

```bash
git add -A && git commit -m "feat: 差集提取与翻译作业构建"
```

### 任务 6：占位符保护

**文件：**
- 创建：`backend/app/placeholder.py`
- 测试：`backend/tests/test_placeholder.py`

**接口：**
- 产生：`protect(text: str) -> tuple[str, list[str]]`、`restore(masked: str, markers: list[str]) -> str`

- [ ] **步骤 1：编写失败测试**

`backend/tests/test_placeholder.py`：
```python
from app.placeholder import protect, restore

def test_protect_keeps_format_codes():
    masked, markers = protect("铁锭 §a已获得 %1$s 个 {item} <item:iron_ingot> {{x}}")
    # 占位符被替换成 %%MC_ 标记，普通词保留
    assert "铁锭" in masked and "已获得" in masked
    assert masked.count("%%MC_") == 5 and markers
    for s in ("§a", "%1$s", "{item}", "<item:iron_ingot>", "{{x}}"):
        assert s not in masked

def test_restore_roundtrip():
    text = "got %s of §biron"
    masked, markers = protect(text)
    assert restore(masked, markers) == text

def test_restore_tolerates_bad_index():
    assert restore("x %%MC_99%% y", ["a"]) == "x %%MC_99%% y"
```

- [ ] **步骤 2：运行确认失败** → 预期 FAIL

- [ ] **步骤 3：实现**

`backend/app/placeholder.py`：
```python
import re

# 借鉴 mc_translator(text_processing.rs) 的占位符保护思想，Python 化实现
_PLACEHOLDER_RE = re.compile(
    r"§[0-9a-fk-or]"                  # MC 格式码 §a
    r"|&[0-9a-fk-or]"                 # & 格式码
    r"|%(\d+\$)?[a-zA-Z%]"            # %s %1$s %%
    r"|#(?:[0-9a-fA-F]{3}){1,2}"      # #FFF / #FFFFFF
    r"|\{[^{}]*\}"                    # {var} / {0}
    r"|<[^<>]*>"                      # <item:iron_ingot>
    r"|\{\{.*?\}\}"                   # {{...}}
    r"|\\n"
)

_MARK_RE = re.compile(r"%%MC_(\d+)%%")

def protect(text: str) -> tuple[str, list[str]]:
    """把占位符替换成 %%MC_i%% 标记，返回 (脱敏文本, 原始标记列表)。"""
    markers: list[str] = []

    def _repl(m: re.Match) -> str:
        markers.append(m.group(0))
        return f"%%MC_{len(markers) - 1}%%"

    return _PLACEHOLDER_RE.sub(_repl, text), markers

def restore(masked: str, markers: list[str]) -> str:
    """把 %%MC_i%% 还原回原始占位符；索引越界的标记原样保留。"""
    def _repl(m: re.Match) -> str:
        idx = int(m.group(1))
        if 0 <= idx < len(markers):
            return markers[idx]
        return m.group(0)

    return _MARK_RE.sub(_repl, masked)
```

- [ ] **步骤 4：运行确认通过** → 预期 PASS

- [ ] **步骤 5：提交**

```bash
git add -A && git commit -m "feat: 占位符保护与还原"
```

### 任务 7：资源包生成

**文件：**
- 创建：`backend/app/resourcepack.py`
- 测试：`backend/tests/test_resourcepack.py`

**接口：**
- 产生：`pack_mcmeta(pack_format: int, description: str = "MC Auto Translator") -> dict`、`build_resource_pack(translations: dict[str, dict[str, str]], target_lang: str, pack_format: int, out_path: Path) -> None`

- [ ] **步骤 1：编写失败测试**

`backend/tests/test_resourcepack.py`：
```python
import json, zipfile
from pathlib import Path
from app.resourcepack import pack_mcmeta, build_resource_pack

def test_pack_meta():
    meta = pack_mcmeta(15)
    assert meta["pack"]["pack_format"] == 15

def test_build_resource_pack(tmp_path: Path):
    out = tmp_path / "zh_cn.zip"
    build_resource_pack({"demo": {"item.x": "铁"}}, "zh_cn", 15, out)
    with zipfile.ZipFile(out) as zf:
        assert "pack.mcmeta" in zf.namelist()
        lang = json.loads(zf.read("assets/demo/lang/zh_cn.json"))
        assert lang == {"item.x": "铁"}
```

- [ ] **步骤 2：运行确认失败** → 预期 FAIL

- [ ] **步骤 3：实现**

`backend/app/resourcepack.py`：
```python
import json
import zipfile
from pathlib import Path

def pack_mcmeta(pack_format: int, description: str = "MC Auto Translator") -> dict:
    return {"pack": {"pack_format": pack_format, "description": description}}

def build_resource_pack(translations: dict[str, dict[str, str]], target_lang: str,
                        pack_format: int, out_path: Path) -> None:
    """把 {modid: {key: value}} 生成标准资源包 zip。"""
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("pack.mcmeta", json.dumps(pack_mcmeta(pack_format), ensure_ascii=False, indent=2))
        for modid, entries in translations.items():
            if not entries:
                continue
            zf.writestr(f"assets/{modid}/lang/{target_lang}.json",
                        json.dumps(entries, ensure_ascii=False, indent=2))
```

- [ ] **步骤 4：运行确认通过** → 预期 PASS

- [ ] **步骤 5：提交**

```bash
git add -A && git commit -m "feat: 资源包生成"
```

---

## M2 翻译引擎

### 任务 8：引擎抽象与跳过滤规则

**文件：**
- 创建：`backend/app/translate/__init__.py`、`backend/app/translate/common.py`、`backend/app/translate/engine.py`
- 测试：`backend/tests/test_engine.py`

**接口：**
- 消费：`app.config.AppConfig`、`app.translate.llm.LLMClient`、`app.translate.machine.MachineClient`（任务 9/10 产生，先留类型）
- 产生：`should_translate(text: str) -> bool`、`class TranslationEngine(Protocol)`（`async translate_batch(self, texts: list[str], target_lang: str) -> list[str]`）、`create_engine(cfg: AppConfig) -> TranslationEngine`

- [ ] **步骤 1：编写失败测试**

`backend/tests/test_engine.py`：
```python
import pytest
from app.config import AppConfig
from app.translate.common import should_translate
from app.translate.engine import create_engine

def test_should_translate_filters_technical():
    assert not should_translate("iron_ingot")          # snake_case 标识符
    assert not should_translate("mods/demo/foo.class") # 路径
    assert not should_translate("123")                 # 纯数字
    assert should_translate("How to craft an iron ingot")

def test_create_engine_llm(tmp_path):
    cfg = AppConfig(tmp_path / "c.json")
    cfg.set("engine", "llm")
    from app.translate.llm import LLMClient
    assert isinstance(create_engine(cfg), LLMClient)
```

- [ ] **步骤 2：运行确认失败** → 预期 FAIL

- [ ] **步骤 3：实现**

`backend/app/translate/common.py`：
```python
import re

# 借鉴 mc_translator(skip_rules.rs) 的跳过滤思想
_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_SYMBOL_RE = re.compile(r"^[\W\d_]+$")
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{32,36}$")

def should_translate(text: str) -> bool:
    """判断一段文本是否值得翻译（技术串/标识符/路径/纯数字跳过）。"""
    if not (2 <= len(text) <= 1000):
        return False
    if not re.search(r"[a-zA-Z]", text):
        return False
    if _SYMBOL_RE.match(text):
        return False
    if _IDENT_RE.match(text):            # 标识符 snake_case/camelCase
        return False
    if _UUID_RE.match(text):
        return False
    if text.startswith(("/", "@", "#", "[")):
        return False
    if ":" in text and " " not in text:  # 命名空间 a:b / 文件路径
        return False
    return True
```

`backend/app/translate/engine.py`：
```python
from typing import Protocol
from app.config import AppConfig

class TranslationEngine(Protocol):
    """翻译引擎统一接口。实现必须异步、保持顺序、失败回原文。"""
    async def translate_batch(self, texts: list[str], target_lang: str) -> list[str]: ...

def create_engine(cfg: AppConfig):
    """互斥工厂：engine == "llm" 走 LLMClient，否则走 MachineClient。"""
    if cfg.get("engine") == "llm":
        from app.translate.llm import LLMClient
        l = cfg.get("llm", {})
        return LLMClient(l.get("base_url", ""), l.get("api_key", ""),
                         l.get("model", "deepseek-chat"),
                         concurrency=cfg.get("concurrency", 5))
    from app.translate.machine import MachineClient
    return MachineClient(cfg.get("machine", {}).get("provider", "google"))
```

- [ ] **步骤 4：运行确认通过**

运行：`cd backend && python -m pytest tests/test_engine.py -v`
预期：PASS（测试 9 的 `test_create_engine_llm` 依赖任务 9 的 `LLMClient`，若 LLMClient 未实现会 FAIL——**本任务先实现 common 与 engine，llm.py 在任务 9 落地**；若需本任务全绿，可将测试中 `isinstance` 断言推迟到任务 9 后运行）

- [ ] **步骤 5：提交**

```bash
git add -A && git commit -m "feat: 翻译引擎抽象与跳过滤规则"
```

### 任务 9：LLM 翻译客户端

**文件：**
- 创建：`backend/app/translate/llm.py`
- 测试：`backend/tests/test_llm.py`

**接口：**
- 消费：`app.translate.common.should_translate`、`app.placeholder.protect/restore`
- 产生：`class LLMClient` — `__init__(self, base_url: str, api_key: str, model: str, concurrency: int = 5)`、`async translate_batch(self, texts: list[str], target_lang: str) -> list[str]`

- [ ] **步骤 1：编写失败测试**（用 httpx MockTransport）

`backend/tests/test_llm.py`：
```python
import pytest
from httpx import AsyncClient, MockTransport, Response
from app.translate.llm import LLMClient

def _fake_handler(request):
    body = request.json()
    msg = body["messages"][-1]["content"]
    return Response(200, json={"choices": [{"message": {"content": f"[{msg}]"}}]})

@pytest.mark.asyncio
async def test_translate_batch():
    transport = MockTransport(_fake_handler)
    client = LLMClient("https://x", "k", "m", concurrency=2)
    client._client = AsyncClient(transport=transport)
    out = await client.translate_batch(["hello", "world"], "zh_cn")
    assert out == ["[hello]", "[world]"]
    await client._client.aclose()

@pytest.mark.asyncio
async def test_technical_string_unchanged():
    transport = MockTransport(_fake_handler)
    client = LLMClient("https://x", "k", "m")
    client._client = AsyncClient(transport=transport)
    out = await client.translate_batch(["iron_ingot", "铁块"], "zh_cn")
    assert out == ["iron_ingot", "铁块"]   # 技术串跳过，不调 API
    await client._client.aclose()
```

- [ ] **步骤 2：运行确认失败** → 预期 FAIL（`ModuleNotFoundError` / `No module pytest.mark.asyncio`——若缺 pytest-asyncio，先在 `requirements.txt` 加 `pytest-asyncio`）

- [ ] **步骤 3：实现**

`backend/app/translate/llm.py`：
```python
import asyncio
import httpx
from app.translate.common import should_translate
from app.placeholder import protect, restore

class LLMClient:
    """OpenAI 兼容 /chat/completions 客户端。并发批处理，失败回原文。"""

    def __init__(self, base_url: str, api_key: str, model: str, concurrency: int = 5):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.concurrency = concurrency
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=60,
                                             headers={"Authorization": f"Bearer {self.api_key}"})
        return self._client

    async def translate_batch(self, texts: list[str], target_lang: str) -> list[str]:
        results: list[str] = [""] * len(texts)
        sem = asyncio.Semaphore(self.concurrency)
        client = self._get_client()

        async def work(i: int, text: str) -> None:
            async with sem:
                results[i] = await self._translate_one(client, text, target_lang)

        await asyncio.gather(*(work(i, t) for i, t in enumerate(texts)))
        return results

    async def _translate_one(self, client: httpx.AsyncClient, text: str, target_lang: str) -> str:
        if not should_translate(text):
            return text
        masked, markers = protect(text)
        try:
            resp = await client.post(f"{self.base_url}/chat/completions", json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": f"把 Minecraft 游戏文本翻译成 {target_lang}，"
                                                  f"保留所有 %%MC_数字%% 占位符原样，只输出译文，不要任何解释。"},
                    {"role": "user", "content": masked},
                ],
                "temperature": 0.2,
            })
            resp.raise_for_status()
            translated = resp.json()["choices"][0]["message"]["content"].strip()
            return restore(translated, markers)
        except Exception:
            return text
```

- [ ] **步骤 4：运行确认通过**

运行：`cd backend && pip install pytest-asyncio && python -m pytest tests/test_llm.py -v`
预期：PASS

- [ ] **步骤 5：提交**

```bash
git add -A && git commit -m "feat: OpenAI 兼容 LLM 翻译客户端"
```

### 任务 10：在线机翻客户端

**文件：**
- 创建：`backend/app/translate/machine.py`
- 测试：`backend/tests/test_machine.py`

**接口：**
- 消费：`app.translate.common.should_translate`
- 产生：`class MachineClient` — `__init__(self, provider: str = "google")`、`async translate_batch(self, texts: list[str], target_lang: str) -> list[str]`

- [ ] **步骤 1：编写失败测试**（monkeypatch deep_translator）

`backend/tests/test_machine.py`：
```python
import pytest
from app.translate.machine import MachineClient, map_lang

def test_map_lang():
    assert map_lang("zh_cn") == "zh-CN"
    assert map_lang("en_us") == "en"

@pytest.mark.asyncio
async def test_translate_batch_uses_executor(monkeypatch):
    calls = []
    def fake_translate(src, tgt, text):
        calls.append((src, tgt, text))
        return "译文"
    import deep_translator
    monkeypatch.setattr(deep_translator, "GoogleTranslator",
                        lambda source, target: type("GT", (), {"translate": lambda self, t: fake_translate(source, target, t)})())
    client = MachineClient()
    out = await client.translate_batch(["hello"], "zh_cn")
    assert out == ["译文"] and len(calls) == 1
```

- [ ] **步骤 2：运行确认失败** → 预期 FAIL

- [ ] **步骤 3：实现**

`backend/app/translate/machine.py`：
```python
import asyncio
from deep_translator import GoogleTranslator
from app.translate.common import should_translate

_LANG_MAP = {"zh_cn": "zh-CN", "zh_tw": "zh-TW", "en_us": "en",
             "ja_jp": "ja", "ko_kr": "ko", "fr_fr": "fr", "de_de": "de"}

def map_lang(mc_lang: str) -> str:
    """MC 语言代码 → Google 语言代码。"""
    return _LANG_MAP.get(mc_lang, mc_lang)

class MachineClient:
    """在线机翻（deep-translator 免费通道）。失败回原文。"""

    def __init__(self, provider: str = "google"):
        self.provider = provider

    async def translate_batch(self, texts: list[str], target_lang: str) -> list[str]:
        lang = map_lang(target_lang)
        loop = asyncio.get_running_loop()
        results: list[str] = []
        for t in texts:
            if not should_translate(t):
                results.append(t)
                continue
            try:
                results.append(await loop.run_in_executor(
                    None, GoogleTranslator(source="auto", target=lang).translate, t))
            except Exception:
                results.append(t)
        return results
```

- [ ] **步骤 4：运行确认通过**

运行：`cd backend && python -m pytest tests/test_machine.py -v`
预期：PASS（若真实调 GoogleTranslator 失败，测试走 monkeypatch 不受影响）

- [ ] **步骤 5：提交**

```bash
git add -A && git commit -m "feat: 在线机翻客户端"
```

### 任务 11：术语库

**文件：**
- 创建：`backend/app/glossary.py`
- 测试：`backend/tests/test_glossary.py`

**接口：**
- 产生：`load_glossary(path: Path) -> dict[str, str]`、`term_inject_prompt(glossary: dict[str, str], limit: int = 30) -> str`

- [ ] **步骤 1：编写失败测试**

`backend/tests/test_glossary.py`：
```python
import json
from pathlib import Path
from app.glossary import load_glossary, term_inject_prompt

def test_load(tmp_path: Path):
    p = tmp_path / "g.json"
    p.write_text(json.dumps({"iron": "铁", "diamond": "钻石"}), encoding="utf-8")
    assert load_glossary(p) == {"iron": "铁", "diamond": "钻石"}

def test_missing_file_returns_empty(tmp_path: Path):
    assert load_glossary(tmp_path / "nope.json") == {}

def test_inject_prompt_limited():
    g = {f"k{i}": f"v{i}" for i in range(50)}
    prompt = term_inject_prompt(g, limit=10)
    assert prompt.count("=>") == 10
```

- [ ] **步骤 2：运行确认失败** → 预期 FAIL

- [ ] **步骤 3：实现**

`backend/app/glossary.py`：
```python
import json
from pathlib import Path

def load_glossary(path: Path) -> dict[str, str]:
    """加载术语表 json（{原文: 译文}），文件不存在返回空表。"""
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))

def term_inject_prompt(glossary: dict[str, str], limit: int = 30) -> str:
    """把术语表拼进提示词，最多取前 limit 条。"""
    items = list(glossary.items())[:limit]
    if not items:
        return ""
    lines = [f"{k} => {v}" for k, v in items]
    return "术语表（翻译必须遵守）：\n" + "\n".join(lines)
```

- [ ] **步骤 4：运行确认通过** → 预期 PASS

- [ ] **步骤 5：提交**

```bash
git add -A && git commit -m "feat: 术语库加载与提示词注入"
```

### 任务 12：任务状态与断点存储

**文件：**
- 创建：`backend/app/tasks.py`
- 测试：`backend/tests/test_tasks.py`

**接口：**
- 产生：`@dataclass TaskState`（`id/status/total/done/failed/progress/created_at`）、`class TaskStore` — `new() -> TaskState`、`save(state: TaskState) -> None`、`load(task_id: str) -> TaskState | None`、`list() -> list[TaskState]`

- [ ] **步骤 1：编写失败测试**

`backend/tests/test_tasks.py`：
```python
from pathlib import Path
from app.tasks import TaskStore

def test_new_and_persist(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks")
    t = store.new()
    t.status = "running"
    t.total = 10
    store.save(t)
    loaded = store.load(t.id)
    assert loaded is not None and loaded.status == "running" and loaded.total == 10

def test_list_returns_saved(tmp_path: Path):
    store = TaskStore(tmp_path / "tasks")
    store.new()
    store.new()
    assert len(store.list()) == 2
```

- [ ] **步骤 2：运行确认失败** → 预期 FAIL

- [ ] **步骤 3：实现**

`backend/app/tasks.py`：
```python
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path

@dataclass
class TaskState:
    id: str
    status: str = "pending"          # pending/running/done/failed/paused
    total: int = 0
    done: int = 0
    failed: int = 0
    progress: list[dict] = field(default_factory=list)  # [{key, source, translated, status}]
    created_at: float = field(default_factory=time.time)

class TaskStore:
    """任务状态 json 持久化，断点续翻靠它。"""

    def __init__(self, dir: Path):
        self.dir = dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.json"

    def new(self) -> TaskState:
        state = TaskState(id=uuid.uuid4().hex[:12])
        self.save(state)
        return state

    def save(self, state: TaskState) -> None:
        self._path(state.id).write_text(json.dumps(asdict(state), ensure_ascii=False), encoding="utf-8")

    def load(self, task_id: str) -> TaskState | None:
        p = self._path(task_id)
        if not p.exists():
            return None
        return TaskState(**json.loads(p.read_text(encoding="utf-8")))

    def list(self) -> list[TaskState]:
        return [self.load(p.stem) for p in self.dir.glob("*.json") if p.stem]
```

- [ ] **步骤 4：运行确认通过** → 预期 PASS

- [ ] **步骤 5：提交**

```bash
git add -A && git commit -m "feat: 任务状态与断点持久化"
```

---

## M3 后端 API 与前端（任务 13-14）

### 任务 13：FastAPI 路由

**文件：**
- 创建：`backend/app/main.py`、`backend/app/models.py`
- 测试：`backend/tests/test_api.py`

**接口：**
- 消费：`app.config.AppConfig`、`app.scanner.scan_modpack/scan_jar`、`app.translate.engine.create_engine`、`app.diff.build_jobs`、`app.resourcepack.build_resource_pack`、`app.tasks.TaskStore`
- 产生：FastAPI app，路由：`GET /api/config`、`POST /api/config`、`POST /api/scan`（body `{mode:"modpack"|"jar", path}`）、`POST /api/translate`（body `{path, mode, target_lang}`）、`GET /api/task/{tid}`、`GET /api/task/{tid}/download`

**实现要点：**（本任务代码由执行者按下列契约补全，模型与路由真实实现）
- `models.py`：Pydantic `ScanRequest`、`TranslateRequest`、`ScanResponse`
- `main.py`：`app = FastAPI(title="像素译站")`；配置读写直接走 `AppConfig("config.json")`；`/api/scan` 同步扫描返回 ModScan 的 JSON 化列表；`/api/translate` 用 `asyncio` 后台任务跑 `create_engine` 逐批翻译并写入 `TaskStore`；`/api/task/{tid}` 返回任务进度；下载端点把生成的资源包 zip 用 `FileResponse` 返回
- 测试：用 `fastapi.testclient.TestClient` 打 `POST /api/config` 与 `POST /api/scan`（临时目录造 jar）

- [ ] 步骤 1：写失败测试（TestClient 打 config/scan）
- [ ] 步骤 2：运行确认失败
- [ ] 步骤 3：实现 models.py 与 main.py
- [ ] 步骤 4：运行确认通过
- [ ] 步骤 5：提交 `git commit -m "feat: FastAPI 路由与任务执行"`

### 任务 14：Vue3 前端骨架

**文件：**
- 创建：`frontend/package.json`、`frontend/vite.config.js`、`frontend/index.html`、`frontend/src/main.js`、`frontend/src/App.vue`、`frontend/src/api.js`、`frontend/src/views/SetupView.vue`、`frontend/src/views/ProgressView.vue`

**实现要点：**
- Vite + Vue3；`api.js` 封装 fetch（`/api/config`、`/api/scan`、`/api/translate`、`/api/task/{id}` 轮询）
- `SetupView.vue`：引擎互斥单选（LLM API 填 base_url/key/model / 在线机翻）、目标语言下拉（默认 zh_cn）、模式选择（整合包目录 / 单 jar）、目录选择
- `ProgressView.vue`：任务进度条 + 失败列表 + 下载按钮（`/api/task/{id}/download`）
- dev 通过 Vite proxy 转发 `/api` 到 `http://127.0.0.1:8000`
- 验收（手动）：`npm install && npm run dev`，连后端后走通「选目录 → 扫描 → 翻译 → 下载资源包」

- [ ] 步骤 1：搭 Vite 项目与 proxy
- [ ] 步骤 2：实现 SetupView（引擎互斥选择 + 表单）
- [ ] 步骤 3：实现 ProgressView + api.js 轮询
- [ ] 步骤 4：本地联调验收
- [ ] 步骤 5：提交 `git commit -m "feat: Vue3 前端骨架与翻译流程页"`

---

## M4-M6 后续子计划（边界说明，实施到 M3 后再单独细化）

### M4 地图汉化（依赖 nbtlib）
- 前置研究：审计 `_upstream/MCC-i18n` 的 `utils/nbt_helper.py`、`workers/scan_worker.py`、`workers/write_worker.py`（MIT，可直接拷贝/提取纯函数）
- 落地模块：`backend/app/maps/`（世界识别、NBT 递归扫描、写回、.bak 备份）
- 已知局限：MCC-i18n 的 MCA 是字节正则 hack、告示牌无专门实现——生产级需评估 `anvil`/`python-amulet`，产出独立计划

### M5 字节码硬编码直改（自研，参考 xtmc 算法）
- 前置：按 JVM 规范自研 `ClassFileModifier`（常量池解析/Utf8 改写/重建，仅用 `struct`），参考 xtmc `backend/main.py:108` 算法但不拷贝代码
- 落地模块：`backend/app/bytecode/`，含写入前快照备份
- 产出独立计划后再动工

### M6 打包为安装应用（最后 debug 完才做）
- 两个版本：① pywebview 加载前端构建产物 + PyInstaller onedir + Inno Setup 出安装程序 ② PyInstaller `--onefile` 出便携版
- 脚本：`scripts/build_installer.ps1`；桌面壳 `backend/app/desktop.py`（pywebview.create_window 指向 FastAPI 本地服务或静态文件）
- 交付物：`dist/` 下安装包 + 便携 exe

---

## 自检清单（本计划）

1. **规范覆盖**：整合包/单 mod 汉化（T4/T5）、地图汉化（M4）、硬编码（M5）、AI 与机翻互斥（T8-T10）、目标语言参数化（全局约束）、单应用打包（M6）、可安装应用（M6）——全部有对应落点
2. **占位符扫描**：任务 8 的 `test_engine.py` 中 `isinstance(create_engine(cfg), LLMClient)` 依赖任务 9，已注明执行顺序；无其他 TBD/TODO
3. **类型一致性**：`TranslationEngine` 协议签名统一为 `translate_batch(texts, target_lang) -> list[str]`，T8-T10 一致；`ModScan`/`TranslationJob`/`TaskState` 字段在 T4/T5/T12 定义并在 T13 消费，命名一致
4. **许可证**：所有直接复用仅限 MIT（MCC-i18n 模块与算法），详见 `docs/UPSTREAM.md`

---

## V2 修订（第二轮头脑风暴后，实施时以本节为准）

> 基于自审发现追加。全局约束增补 + 对任务 4/7/9/12/13 的修订 + 两个新文件（`version.py`、`memory.py`）。

### 全局约束增补

- API key 用 `keyring` 加密存储，`config.json` 绝不落明文；config 只存 `base_url`/`model`
- pack_format 与语言文件格式随 MC 版本适配：`pack_format ≥ 4`（1.13+）输出 `.json`，否则输出 `.lang`
- 翻译记忆 `{原文→译文}` 持久化复用，已翻过的不再调用引擎（断点续翻的真正闭环）
- 扫描默认限定 `mods/` 目录（`scope="mods"`），避免误扫 resourcepacks/saves
- LLM 用批次拼接翻译 + 降级链（全量批→半批→逐条），输出必须经过结果清洗

### 对任务 4 的修订：扫描范围（已含在上文实现）

`scan_modpack(dir, source_lang, target_lang, scope="mods")` 已增加 `scope` 参数。补一条测试：

```python
def test_scan_modpack_scope(tmp_path: Path):
    mods = tmp_path / "mods"; mods.mkdir()
    _jar(mods / "m1.jar", "m1", {"k": "v"}, {})
    _jar(tmp_path / "stray.jar", "stray", {"k": "v"}, {})   # 放在 mods 外，不应被扫到
    scans = scan_modpack(tmp_path, "en_us", "zh_cn")         # scope 默认 "mods"
    assert [s.modid for s in scans] == ["m1"]
```

### 对任务 7 的修订：版本适配（新增 `backend/app/version.py`）

**文件：** 创建 `backend/app/version.py`、测试 `backend/tests/test_version.py`

**接口：** `version_to_pack_format(version: str) -> int`、`pack_format_to_lang_ext(pack_format: int) -> str`

`backend/app/version.py`：
```python
# MC 版本 → 资源包格式版本（pack_format）已知映射
_KNOWN: dict[str, int] = {
    "1.12.2": 3, "1.13.2": 4, "1.14.4": 4, "1.15.2": 5,
    "1.16.5": 6, "1.17.1": 7, "1.18.2": 9, "1.19.2": 12,
    "1.20.1": 15, "1.20.4": 22, "1.21": 34, "1.21.4": 46,
    "1.21.5": 55,
}

def version_to_pack_format(version: str) -> int:
    """MC 版本字符串 → pack_format；未知版本回退 15（1.20.1）。"""
    return _KNOWN.get(version, 15)

def pack_format_to_lang_ext(pack_format: int) -> str:
    """pack_format ≥ 4（1.13+）用 .json；1.12 及以下用 .lang。"""
    return "json" if pack_format >= 4 else "lang"
```

`backend/tests/test_version.py`：
```python
from app.version import version_to_pack_format, pack_format_to_lang_ext

def test_known_versions():
    assert version_to_pack_format("1.20.1") == 15
    assert version_to_pack_format("1.12.2") == 3

def test_lang_ext_boundary():
    assert pack_format_to_lang_ext(3) == "lang"   # 1.12
    assert pack_format_to_lang_ext(4) == "json"   # 1.13+

def test_unknown_version_fallback():
    assert version_to_pack_format("9.9.9") == 15
```

`resourcepack.py` 修订：`build_resource_pack` 增参 `lang_ext: str = "json"`，输出文件名用 `<target>.<lang_ext>`：

```python
from app.version import pack_format_to_lang_ext

def build_resource_pack(translations, target_lang: str, pack_format: int, out_path: Path) -> None:
    ext = pack_format_to_lang_ext(pack_format)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("pack.mcmeta", json.dumps(pack_mcmeta(pack_format), ensure_ascii=False, indent=2))
        for modid, entries in translations.items():
            if not entries:
                continue
            zf.writestr(f"assets/{modid}/lang/{target_lang}.{ext}",
                        json.dumps(entries, ensure_ascii=False, indent=2))
```

### 对任务 9 的修订：批次拼接翻译 + 降级链 + 结果清洗

`backend/app/translate/llm.py` 升级：新增 `build_tagged_texts` / `parse_tagged` / `clean_translation`（放 `llm.py` 或 `translate/common.py`），`translate_batch` 走批次。

新增测试（`backend/tests/test_llm.py` 追加）：
```python
from app.translate.llm import build_tagged_texts, parse_tagged, clean_translation

def test_tagged_roundtrip():
    prompt = build_tagged_texts(["a", "b", "c"])
    parsed = parse_tagged("[i0] 甲\n[i1] 乙\n[i2] 丙")
    assert parsed == {0: "甲", 1: "乙", 2: "丙"}

def test_clean_translation():
    assert clean_translation("```\n翻译：铁锭\n```") == "铁锭"
    assert clean_translation('"铁块"') == "铁块"
```

实现（新增进 `llm.py`）：
```python
import re

_TAG_RE = re.compile(r"^\[i(\d+)\]\s*", re.MULTILINE)

def build_tagged_texts(texts: list[str]) -> str:
    """N 条文本拼一条 prompt，每行带 [i索引] 前缀，便于切回。"""
    return "\n".join(f"[i{i}] {t}" for i, t in enumerate(texts))

def parse_tagged(translated: str) -> dict[int, str]:
    """解析模型按 [iN] 标签输出的结果。"""
    out: dict[int, str] = {}
    cur_idx: int | None = None
    parts: list[str] = []
    for line in translated.splitlines():
        m = _TAG_RE.match(line)
        if m:
            if cur_idx is not None:
                out[cur_idx] = "\n".join(parts).strip()
            cur_idx, parts = int(m.group(1)), [line[m.end():].strip()]
        elif cur_idx is not None:
            parts.append(line)
    if cur_idx is not None:
        out[cur_idx] = "\n".join(parts).strip()
    return out

def clean_translation(raw: str) -> str:
    """清洗 LLM 输出：剥代码块/翻译前缀/首尾引号。"""
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n", "", s)
        s = re.sub(r"\n?```$", "", s)
    s = re.sub(r"^(翻译|译文|结果|Translation)\s*[:：]\s*", "", s)
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'“”":
        s = s[1:-1]
    return s.strip()
```

`translate_batch` 改为降级链（替换原实现核心）：
```python
async def translate_batch(self, texts, target_lang):
    results: list[str] = [""] * len(texts)
    todo = [(i, t) for i, t in enumerate(texts) if should_translate(t)]
    if not todo:
        return list(texts)
    await self._translate_whole(self._get_client(), todo, target_lang, results)
    return results

async def _translate_whole(self, client, todo, target_lang, results):
    """全量一批；失败切半批重试；仍失败逐条；逐条失败回原文。"""
    idxs = [i for i, _ in todo]
    masked, markers = zip(*[protect(t) for _, t in todo])
    prompt = build_tagged_texts(list(masked))
    try:
        resp = await client.post(f"{self.base_url}/chat/completions", json={
            "model": self.model,
            "messages": [{"role": "system", "content":
                f"把 Minecraft 游戏文本翻译成 {target_lang}。每行以 [i数字] 开头，"
                f"输出保持 [i数字] 前缀和 %%MC_数字%% 占位符原样，只输出译文。"},
                {"role": "user", "content": prompt}],
            "temperature": 0.2,
        })
        resp.raise_for_status()
        out = resp.json()["choices"][0]["message"]["content"]
    except Exception:
        out = None
    if out is not None:
        parsed = parse_tagged(out)
        for n, (i, _) in enumerate(todo):
            if n in parsed:
                results[i] = restore(clean_translation(parsed[n]), markers[n])
    missing = [i for i, r in zip(idxs, [results[i] for i, _ in todo]) if not r]
    if len(todo) > 1 and missing:
        half = todo[:len(todo)//2]
        await self._translate_whole(client, half, target_lang, results)
        await self._translate_whole(client, todo[len(todo)//2:], target_lang, results)
    for i, t in todo:
        if not results[i]:
            results[i] = await self._translate_one(client, t, target_lang)
```

### 对任务 12 的修订：翻译记忆 + 取消标志（新增 `backend/app/memory.py`）

**文件：** 创建 `backend/app/memory.py`、测试 `backend/tests/test_memory.py`；`tasks.py` 的 `TaskState` 加 `paused`/`cancelled` 字段。

`backend/app/memory.py`：
```python
import json
from pathlib import Path

class MemoryStore:
    """翻译记忆：{原文: 译文} 持久化。翻译前先查记忆，命中直接填，miss 才调引擎。"""
    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, str] = {}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))

    def get(self, source: str) -> str | None:
        return self.data.get(source)

    def set(self, source: str, translated: str) -> None:
        self.data[source] = translated

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
```

`backend/tests/test_memory.py`：
```python
from app.memory import MemoryStore

def test_memory_roundtrip(tmp_path):
    m = MemoryStore(tmp_path / "mem.json")
    assert m.get("hi") is None
    m.set("hi", "嗨")
    m.save()
    m2 = MemoryStore(tmp_path / "mem.json")
    assert m2.get("hi") == "嗨"
```

`tasks.py` 修订（`TaskState` 增字段）：
```python
@dataclass
class TaskState:
    id: str
    status: str = "pending"          # pending/running/done/failed/paused
    total: int = 0
    done: int = 0
    failed: int = 0
    paused: bool = False
    cancelled: bool = False
    progress: list[dict] = field(default_factory=list)  # [{key, source, translated, status}]
    created_at: float = field(default_factory=time.time)
```

翻译循环（任务 13 内实现）须遵守：每批前检查 `paused`/`cancelled`；每条先 `memory.get(source)`，命中直接填入并计数，miss 才走引擎，成功即 `memory.set` 并周期 `memory.save()`。

### 对任务 13 的修订：API 增补

- 新增 `POST /api/task/{tid}/cancel`、`POST /api/task/{tid}/pause`（改 `TaskState.paused/cancelled`）
- 新增 `GET /api/browse?path=...`：返回 `{parent, dirs:[子目录名]}`，根路径时返回盘符列表（开发期浏览器选目录）
- API key 读写走 `keyring.get_password("mc-translator", "api_key")` / `keyring.set_password(...)`，config 只存 `llm.base_url`/`llm.model`
- 新增 `POST /api/glossary`（上传/合并术语表到 `glossary.json`）

### 测试矩阵增补

| 新测试文件 | 覆盖 |
|---|---|
| `tests/test_version.py` | 版本→pack_format、格式边界（lang/json） |
| `tests/test_memory.py` | 记忆持久化往返 |
| `tests/test_llm.py`（追加） | 批次拼接/切回、清洗、降级链（MockTransport 返回缺失行 → 逐条兜底） |
| `tests/test_scanner.py`（追加） | scope="mods" 不误扫外部 jar |

### 依赖增补

`backend/requirements.txt` 增加：`pytest-asyncio`、`keyring`（已并入任务 1 清单，若此前已装则忽略）。

---

## V3 最终决策整合（五阶段头脑风暴后，实施时以本节为准）

> 用户 2026-08-09 五阶段逐个拍板 19 项决策。与 V1/V2 冲突处一律以本节为准。

### 决策总表

| 主题 | 决策 |
|---|---|
| 输入形态 | 文件夹目录 + `.zip`/`.mrpack` 压缩包都要（压缩包自动解压临时目录再扫） |
| MC 版本 | UI 下拉手动选（默认 1.20.1）+ 从整合包自动探测，探测不准回退手动 |
| 语言文件输出 | 资源包为主 + 「直写 mod jar」可选 |
| 简繁转换 | 要：zh_cn↔zh_tw 用 OpenCC 免 AI 直转 |
| LLM 预置模板 | DeepSeek / 通义千问 / Kimi + Ollama + OpenAI 兼容自定义 |
| 免费机翻源 | deep-translator 多源可配置，默认 Google（国内可切 Bing） |
| 成本控制 | 翻译记忆 + token/成本统计 + RPM 限速 + 并发批大小可调，**且自动带智能默认值** |
| 词库 | CFPA 官方词库 + 用户自定义（最高优先）+ 翻译记忆 三层 |
| 工作流 | 分步向导 + 侧边栏导航 |
| 校对台 | 完整版：diff 预览、内联编辑、批量采纳/回滚、失败重试、状态筛选 |
| 目录选择 | 文本框 + `/api/browse` 目录浏览器；桌面打包后叠加 pywebview 原生对话框 |
| 进度展示 | 进度条 + 明细列表 + 实时日志面板 |
| 字节码深度 | 仅常量池 Utf8 替换（M5） |
| 地图文本范围 | 全捞：命令方块/Boss栏/告示牌/书与笔/讲台/实体与展示框自定义名/进度成就触发器/其他 NBT 文本键 |
| 地图输出 | **导出新存档**（整档复制后改副本，原档只读不动） |
| 打包 | 安装版（Inno Setup）+ 便携版（PyInstaller onefile）双版本 |
| 许可证 | MIT（README 致谢 MCC-i18n/mc_translator MIT、zVictorium CC BY-NC） |
| CLI | 仅 GUI，不做命令行入口 |
| 目标语言 | 架构任意语言参数化 + UI 下拉默认 zh_cn，列常见语言 |

### 新增模块与代码

#### 整合包压缩包支持 —— `backend/app/archive.py`

**接口：** `is_archive(path: Path) -> bool`、`extract_modpack(archive: Path, dest: Path) -> Path`

```python
import zipfile
from pathlib import Path

def is_archive(path: Path) -> bool:
    """是否整合包压缩包（zip / mrpack）。"""
    return path.suffix.lower() in (".zip", ".mrpack")

def extract_modpack(archive: Path, dest: Path) -> Path:
    """解压整合包压缩包到 dest（自动建目录），返回解压根目录。
    zip 与 mrpack 同为 zip 容器；mrpack 根含 mods/、overrides/，解压后按普通目录扫。"""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(dest)
    return dest
```

测试：构造一个 zip（内放 `mods/m1.jar`），`is_archive==True`，解压后 `dest/mods/m1.jar` 存在。

#### LLM 厂商预置模板 —— `backend/app/translate/providers.py`

**接口：** `PROVIDERS: dict[str, dict]`、`smart_defaults(provider: str) -> dict`

```python
# 厂商预置：base_url/model + 智能并发/批大小默认值（"自动设置"的落地 = 预置推荐值，用户可覆盖）
PROVIDERS: dict[str, dict] = {
    "DeepSeek":   {"base_url": "https://api.deepseek.com", "model": "deepseek-chat",
                   "concurrency": 8, "batch_size": 20},
    "通义千问":     {"base_url": "https://dashscope.aliyuncs.com/compatible-mode",
                   "model": "qwen-plus", "concurrency": 6, "batch_size": 20},
    "Kimi":       {"base_url": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k",
                   "concurrency": 5, "batch_size": 20},
    "Ollama":     {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:7b",
                   "concurrency": 2, "batch_size": 10},
    "自定义":       {"base_url": "", "model": "", "concurrency": 5, "batch_size": 20},
}

def smart_defaults(provider: str) -> dict:
    """返回该厂商智能默认并发/批大小；未知厂商回退"自定义"。"""
    return PROVIDERS.get(provider, PROVIDERS["自定义"])
```

`create_engine` 修订：config 记 `provider` 名；未显式填并发/批大小时用 `smart_defaults(provider)` 填充。前端厂商下拉选中后自动带出 base_url/model/并发/批大小，用户可改。

#### 简繁直转 —— `backend/app/translate/han.py`

**接口：** `simplify(text: str) -> str`、`traditional(text: str) -> str`、`is_same_script(src: str, tgt: str) -> bool`

```python
from opencc import OpenCC

_t2s = OpenCC("t2s")
_s2t = OpenCC("s2t")

def simplify(text: str) -> str:
    return _t2s.convert(text)

def traditional(text: str) -> str:
    return _s2t.convert(text)

def is_same_script(src: str, tgt: str) -> bool:
    """源/目标同为 zh_cn/zh_tw 时，走简繁直转，跳过 AI（省钱）。"""
    return {src, tgt} <= {"zh_cn", "zh_tw"}
```

翻译循环接入：目标语言 `zh_tw` 且源语言为中文 → 对已译简体直接 `traditional()` 转换，不调引擎。依赖增补 `opencc-python-reimplemented`。

#### token/成本统计 —— `llm.py` 修订

- `_translate_one`/`_translate_whole` 读取 `resp.json().get("usage", {})`，提取 `prompt_tokens`/`completion_tokens`
- `LLMClient` 增加 `on_usage: Callable[[int, int], None] | None` 回调；`translate_batch` 在每次请求后调用
- `TaskState` 增字段：`tokens_in: int = 0`、`tokens_out: int = 0`、`cost_estimate: float = 0.0`（成本 = 按所选模型的每百万 token 单价估算，单价表存 providers.py）

### 对 M4 的修订：地图汉化改为「导出新存档」

- **流程**：复制整档到输出目录 → 只在副本上扫描与写回 NBT → 输出汉化副本（文件夹 + 可选 mcworld zip），**原档只读不动**
- **扫描范围（全捞）**：NBT 键白名单可配置（`maps/scan_keys.json`），默认包含：`Command`（命令方块）、`CustomName`/`Name`（实体/展示框/盔甲架）、`front_text`/`Text1..Text4`（告示牌，自研）、`pages`/`book`（书与笔/讲台）、`title`/`author`；数据包侧 `data/**/*.json`（进度/成就）与 `.mcfunction`（say/tellraw/title）
- 写回策略沿用 MCC-i18n 的 `replace_in_nbt` 思路（String 值内替换 + JSON 文本 `text` 键），但作用于副本
- 告示牌/书与笔为自研部分（MCC-i18n 无专门实现），参考 NBT 结构：告示牌 `front_text.messages`（1.20+）或 `Text1-4`（旧版）；书 `pages` 是 `List<`String`>`

### 对 M6 的修订：双版本打包

- **桌面壳**：`backend/app/desktop.py` —— pywebview 创建窗口加载「前端构建产物 + FastAPI 本地静态服务」，打包时引入
- **版本 A 安装版**：PyInstaller `onedir` + Inno Setup 脚本（`scripts/build_installer.ps1`）生成安装程序（开始菜单快捷方式、卸载项）
- **版本 B 便携版**：PyInstaller `--onefile` 单文件 exe，即拷即用
- 注意 WebView2 Runtime：安装版检测缺失提示安装；便携版说明依赖系统 WebView2（Win10/11 一般自带）

### 许可证

项目声明 **MIT License**；`README.md` 致谢：MCC-i18n（MIT）、mc_translator（MIT）、借鉴 zVictorium（CC BY-NC 4.0）思路。复用 MCC-i18n 的 `utils/json_validator.py` 等文件时保留其 MIT 版权头。

### 对任务 8/9 的接口一致性修订

- `create_engine(cfg)` 读取 `cfg.get("provider")` 而非仅 `engine`；config 结构变为：
  ```python
  {"engine": "llm", "provider": "DeepSeek", "target_lang": "zh_cn", ...}
  ```
- 前端「LLM 接入」面板：厂商下拉（DeepSeek/通义/Kimi/Ollama/自定义）→ 自动带出模板 → 填 key 即可
- 目标语言下拉默认 `zh_cn`，列出 `zh_cn/zh_tw/en_us/ja_jp/ko_kr/fr_fr/de_de` 等常见项
