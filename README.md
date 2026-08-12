# 像素译站（Pixel Translation Station）

> 一个跨平台（Windows）的 Minecraft 整合包 / Mod / 地图 / 光影 **AI 自动汉化工具**。
> 拖入文件，自动识别类型、翻译、生成可直接使用的汉化产物，全程可视化进度。

**本软件由 AI（Claude Code）辅助编写**，前端 Vue 3 + 后端 FastAPI，打包为 Windows 桌面应用（pywebview）。

---

## 📖 简介

像素译站是一款面向 Minecraft 玩家的**一键汉化工具**。它将整合包 / Mod / 地图 / 光影中的英文文本自动翻译为简体或繁体中文，并生成可直接投入游戏的汉化产物（资源包 / 汉化 jar / 汉化存档 / 汉化光影包），无需手动编辑语言文件。

- **AI 翻译为主**：接入 DeepSeek / 通义 / Kimi / Ollama / 免费 API 等任意 OpenAI 兼容接口
- **社区词库兜底**：内置 CFPA 人工翻译词库，命中直接复用社区公认译名
- **全文本覆盖**：语言文件、教程书（Patchouli）、任务书（FTB Quests）、进度（Advancements）、KubeJS 脚本、硬编码字节码文本
- **自动审查闭环**：AI 裁判 + 三级质量审查 + 目标语言校验 + 占位符保护，杜绝纯英文残留
- **断点续联**：中断后可继续，已翻译内容不重复

---

## 🖥 界面预览

可在线查看主界面交互版预览（纸张工坊风格：动态 hero + 输入面板 + 使用说明 / 免责声明三折叠）：**[ui-preview.html](docs/ui-preview.html)**

## ✨ 功能特性

### 翻译引擎
- 支持任意 OpenAI 兼容接口（DeepSeek / 通义千问 / Kimi / Ollama / 自定义 / 免费平台智谱 · 讯飞）
- 并发 + 批量分档（低/中/高三档吞吐），**自动测试吞吐档位**——从高到低探测当前 API 稳定上限，一键应用
- 在线机翻（Google 免费通道）兜底
- 胡言乱语模式（搞笑 / 热梗翻译但忠实原意）

### 文本选取（智能判定）
- **语言文件**：`en_us.json` / `.lang` / `.properties` 全格式解析，UTF-8 / BOM 兼容
- **差集翻译**：Mod 自带中文的 key 自动跳过，只补真缺的（防重复翻译、省 token）
- **教程书 / 进度**：Patchouli 书、Advancements（只翻 display 文本，不碰 criteria 触发条件）
- **任务书**：FTB Quests 新旧格式（含跨行长段描述数组）、Better Questing
- **脚本**：KubeJS 内 `text` / `title` / `tooltip` 等明确文本字段，不碰代码逻辑串
- **硬编码**：字节码（`.class` 常量池）字符串提取，Logger 指令剔除，AI 裁判三分类（翻译 / 排除 / 待定）

### 质量审查（AI 裁判核心）
- 三级审查流水线：初审 → 强制重翻 → 终审
- **目标语言校验**：译文必须含目标语言字符，纯英文假翻译一律重翻
- **占位符保护**：`%s` / `{var}` / `§颜色码` / `&x RGB` 等翻译前后一致校验，坏译文不落盘
- **名称归一化**：专有名词全包统一（第一定义 + 后续跟随）
- 合理保留规则（命令 / 代码标识 / 资源定位符）不误翻

### 产物形态
| 输入 | 产物 | 说明 |
|------|------|------|
| 整合包 | `整合包汉化.zip` | 解压拷入整合包根目录即用：汉化资源包 + i18n 汉化模组 + Vault Patcher 硬编码补丁 + 任务书 / 配置补丁 + 使用说明 |
| 单个 Mod | 汉化 jar | 语言文件 + 教程书 + 硬编码全写回 |
| 地图 | 汉化存档 | NBT / JSON / mcfunction / 区块重写 |
| 光影 | 汉化光影包 | `shaders/lang/<目标语言>.lang` |

- **pack_format 自动注入**：按整合包真实 MC 版本写入兼容资源包格式（1.16.2 ~ 1.21.8 权威表，1.21.9+ 数组格式），杜绝「材质包不兼容」
- 产物名含「全量简体中文化 · 覆盖率 xx%」

### 资源管理
- **内置资源**：CFPA 社区词库（6 个 MC 版本）、i18n 汉化模组、Vault Patcher 硬编码补丁
- **检查更新**：三项均支持「检查更新」——有更新自动下载到应用目录（持久，清缓存不删），没更新 / 没连上明确提示，多源（官方 + 国内镜像）下载
- 缓存目录可自定义（省 C 盘），一键清除

### 桌面体验
- 拖入即翻，zip 嵌套结构自动下钻
- 实时进度（阶段化：语言文件 → 任务书 → 脚本 → 硬编码 → 打包）+ SSE 推送
- 断点续联只在启动时检测，已完成项目不误显示
- 翻译报告（覆盖率 + 全部未翻译条目及原因）
- 空态三折叠（动态 hero + 使用说明 + 免责声明）

---

## 🛠 技术栈

| 层 | 技术 |
|----|------|
| 后端 | Python 3.14 · FastAPI · asyncio · httpx · nbtlib · jawa（字节码）· opencc（简繁）· deep-translator |
| 前端 | Vue 3 · Vite · pywebview |
| 打包 | PyInstaller（onedir + onefile）· Inno Setup（安装版） |
| 测试 | pytest（370+ 用例） |

---

## 📁 目录结构

```
Minecraft-translate/
├── backend/                # FastAPI 后端
│   ├── app/
│   │   ├── main.py         # API 端点 / 任务调度 / SSE
│   │   ├── auto_flow.py    # 全自动翻译主流程（阶段化）
│   │   ├── text_sources.py # 文本源提取（语言文件/教程书/任务书/脚本）
│   │   ├── hardcode.py     # 字节码硬编码提取 + AI 裁判
│   │   ├── vp.py           # Vault Patcher 补丁生成
│   │   ├── cfpa.py         # CFPA 社区词库
│   │   ├── review.py       # AI 质量审查
│   │   ├── detect.py       # 类型识别 + pack_format 注入
│   │   ├── maps/           # 地图汉化
│   │   ├── translate/      # 翻译引擎（LLM / 机翻 / 厂商预置）
│   │   └── data/           # 内置资源（CFPA 词库 / i18n / VP）
│   └── tests/              # 370+ 测试用例
├── frontend/               # Vue 3 前端
│   └── src/
│       ├── views/          # 输入面板 / 进度工作区 / 设置
│       ├── api.js          # 后端 API 对接
│       └── style.css       # 纸张工坊主题
├── assets/                 # 应用图标（SVG 手绘像素风）
├── scripts/
│   ├── build_all.ps1       # 一键打包脚本
│   ├── mc_translator.spec  # PyInstaller 配置
│   └── installer.iss       # Inno Setup 安装版
└── docs/                   # 文档
```

---

## 🔗 参考的开源项目

本软件在开发过程中研究并参考了以下开源项目（思路借鉴，非代码复用）：

### 直接参考（本地 `_upstream/` 目录）
| 项目 | 参考内容 |
|------|----------|
| [MCC-i18n](https://github.com/MCC-i18n) | 地图文本汉化：世界文件扫描、自动翻译、批量编辑、一键导出 |
| [Minecraft-Mod-Translator](https://github.com/Mai-xiyu/minecraft-mod-translator) | LDC 字节码硬编码提取、技术标识符过滤、术语表 |
| [mc_translator](https://github.com/) | Minecraft Mod 自动翻译（Rust）：多源翻译、配置文件 |
| [XTMC Translate](https://github.com/xtmc-minecraft-mod-translator) | 前后端分离的模组翻译工具：任务书 / 配置翻译 |

### 功能参考
| 项目 | 参考内容 |
|------|----------|
| [Aaalice_Minecraft_Translator](https://github.com/Aaalice233/Aaalice_Minecraft_Translator) | 占位符 / 格式码保护、翻译结果校验（缺失翻译 / 占位符 / 格式异常）、重试队列 |
| [mods-string-extractor](https://github.com/zack-zzq/mods-string-extractor) | `en_us − zh_cn` 差集提取（已有翻译跳过） |
| [Translator-Minecraft](https://github.com/lingxingmiao/Translator-Minecraft) | KubeJS / FTB Quests / Better Questing 翻译、混合编码分离 |
| [FTB Quest Localizer](https://www.mcmod.cn/class/15785.html) | FTB 任务导出为语言文件 |
| [I18nUpdateMod3](https://github.com/CFPAOrg/I18nUpdateMod3) | CFPA 汉化资源包自动下载 / 合并 / 应用 |
| [Vault Patcher](https://github.com/3093FengMing/VaultPatcher) | 字节码硬编码运行时替换（pairs / i18n / dynamic 模块格式） |
| [Nixinova/pack-format](https://github.com/Nixinova/pack-format) | MC 版本 → pack_format 权威映射 |
| [CFPAOrg Minecraft-Mod-Language-Package](https://github.com/CFPAOrg/Minecraft-Mod-Language-Package) | 社区人工翻译词库（内置 + 在线下载） |

> `_upstream/` 目录仅作本地研究参考，**不会上传到 GitHub**。

---

## 📥 安装方式

### 方式一：便携版（推荐）
下载 `dist/便携版/像素译站.exe`，双击直接运行，无需安装。配置文件与缓存生成在 exe 同目录。

### 方式二：安装版
下载 `dist/安装版/像素译站-Setup.exe`，运行安装向导，安装到指定目录。

### 方式三：源码运行
```bash
# 后端
cd backend
pip install -r requirements.txt
uvicorn app.main:app --port 8000

# 前端（另开终端）
cd frontend
npm install
npm run dev
```
浏览器打开 `http://localhost:5173`（桌面版会自动拉起 pywebview 窗口）。

> **首次使用**：在「设置」中选择翻译引擎（DeepSeek / 通义 / Kimi / Ollama / 免费平台），填写 API Key（仅保存本机系统凭据库），点击「测试连接」确认，再点「测试吞吐档位」自动选稳定档位。

---

## 🔨 构建方式

### 一键打包（Windows）
```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_all.ps1
```
流程：前端 `npm run build` → PyInstaller（onedir + onefile）→ 整理发布目录 → Inno Setup 安装版（已装则生成 Setup.exe）。

产物：
- `dist/便携版/像素译站.exe` —— 便携版单 exe
- `dist/安装版/像素译站/` —— onedir（安装版源）
- `dist/安装版/像素译站-Setup.exe` —— 安装版（需 Inno Setup 6）

### 手动构建
```bash
# 1. 前端构建
cd frontend
npm run build          # 产出 dist/（供 PyInstaller 打包）

# 2. 后端测试
cd backend
python -m pytest tests/ -q   # 370+ 用例全绿

# 3. PyInstaller 打包（需 frontend/dist 存在）
cd ..
pyinstaller scripts\mc_translator.spec
```

### 环境要求
- Python 3.14 + `pip install -r backend/requirements.txt`
- Node.js 18+ + npm
- PyInstaller（打包用）
- Inno Setup 6（可选，安装版用）

---

## ⚠️ 免责声明

- 本软件为**个人开发、免费开源**，仅供学习交流使用，不得用于任何商业目的。
- 翻译依赖 AI 大模型，**不保证**准确性、完整性、一致性；译文可能存在错译、漏译、机翻腔，请自行核对关键内容。
- 内置 / 下载的社区词库（CFPA）、第三方 Mod（i18n / Vault Patcher）版权归各自作者，仅作离线集成便利。
- 使用本软件可能生成 / 修改资源包、Mod jar、存档、整合包脚本等文件，请**提前备份**原文件；因使用导致的 Mod 加载失败、游戏崩溃、进度丢失等由使用者自行承担。
- 严禁将本软件用于任何非法用途。

---

## 📄 许可证

本项目为开源项目，详见仓库 LICENSE（默认 MIT，以实际 LICENSE 文件为准）。

---

*本 README 由 AI（Claude Code）生成，如有不准确之处请以实际代码为准。*
