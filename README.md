# 像素译站 · Pixel Translation Station

> Minecraft 整合包 / Mod / 地图 / 光影的 **AI 一键汉化工具**。
> 拖入文件 → 自动识别 → 翻译 → 生成可用的汉化产物。全程可视化。
>
> **由 AI（Claude Code）辅助编写 · 免费开源 · Windows 桌面应用**

---

## 📥 快速开始

1. 下载最新版（GitHub **Releases**）：
   - `像素译站.exe` —— 便携版，双击即用，无需安装
   - `像素译站-Setup.exe` —— 安装版
2. 打开应用 → 首次弹出**设置**：选择翻译引擎（DeepSeek / 通义 / Kimi / Ollama / 免费 API）→ 填 API Key → 点「测试连接」
3. 拖入整合包 / Mod / 地图 / 光影 → 点「开始翻译」
4. 完成 → 「打开产物文件夹」→ 解压拷入游戏对应位置即用

> 💡 详细步骤见下方「📖 使用说明」。

---

## ⚠️ 翻译质量 = API 质量（必读）

**本软件是 AI 翻译工具，翻译质量完全取决于你配置的 AI 模型（API）质量。**

| API / 模型 | 预期翻译质量 |
|------------|------------|
| DeepSeek / 通义 / Kimi 等**商业大模型** | 高 —— 准确、流畅、贴合语境 |
| 免费 API（智谱 GLM-Flash / 讯飞 Spark Lite 等） | 中 —— 免费限量限速，质量尚可 |
| 本地 Ollama 小模型 | 低 —— 取决于模型大小，可能机翻腔 |

- 模型越强、上下文越长，译文越自然；小模型 / 免费接口的译文可能有**错译、漏译、机翻腔**。
- 翻译结果**不保证** 100% 准确，重要内容请自行核对。
- 内置的 **CFPA 社区人工词库** 会在语言文件环节**优先命中**，这部分是社区人工校对的高质量译文，与 API 无关；只有词库未覆盖的缺口才走 AI。

---

## 🎯 它能做什么

- ✅ **整合包 / Mod / 地图 / 光影** 全类型一键汉化
- ✅ **AI 翻译 + 社区词库**双通道：词库命中走人工翻译，缺口走 AI
- ✅ **全文本覆盖**：语言文件、教程书、任务书、进度、KubeJS 脚本、硬编码字节码
- ✅ **三级质量审查**：AI 裁判 + 强制重翻 + 目标语言校验，杜绝纯英文残留
- ✅ **断点续联**：中断可继续，已翻译不重复
- ✅ **自动识别 MC 版本**，产物资源包格式正确（游戏不报「不兼容」）

---

## ✨ 功能总览

### 1. 翻译引擎
- 任意 OpenAI 兼容接口：DeepSeek / 通义 / Kimi / Ollama / 自定义 / 免费平台
- 并发 + 批量分档（低 / 中 / 高），**自动测试吞吐档位**（从高到低探测 API 稳定上限）
- 在线机翻（Google）兜底；胡言乱语模式（搞笑热梗翻译但保义）

### 2. 文本覆盖（智能选取）
| 文本类型 | 说明 |
|---------|------|
| 语言文件 | `en_us.json` / `.lang` / `.properties`，UTF-8 / BOM 兼容 |
| 差集翻译 | Mod 自带中文自动跳过，只补真缺的（省 token） |
| 教程书 / 进度 | Patchouli、Advancements（只翻显示文本，不碰触发条件） |
| 任务书 | FTB Quests 新旧格式（含长段描述）、Better Questing |
| KubeJS 脚本 | `text` / `title` / `tooltip` 等明确文本字段 |
| 硬编码 | 字节码字符串提取 + Logger 剔除 + AI 裁判三分类 |

### 3. 质量审查
- 三级流水线：初审 → 强制重翻 → 终审
- **目标语言校验**：译文必须含目标语言字符，纯英文假翻译一律重翻
- **占位符保护**：`%s` / `{var}` / `§颜色码` 翻译前后一致校验
- **名称归一化**：专有名词全包统一（第一定义 + 后续跟随）

### 4. 产物形态
| 输入 | 产物 |
|------|------|
| 整合包 | `整合包汉化.zip`（资源包 + i18n 模组 + VP 硬编码补丁 + 任务书补丁） |
| 单个 Mod | 汉化 jar |
| 地图 | 汉化存档 |
| 光影 | 汉化光影包 |

### 5. 资源管理
- 内置 CFPA 词库（6 版本）、i18n 模组、VP 补丁
- 三项均支持「**检查更新**」：有更新下载到应用目录（持久，清缓存不删），多源下载（官方 + 国内镜像）
- 缓存目录可自定义省 C 盘，一键清除

---

## 🔧 技术栈

| 层 | 技术 |
|----|------|
| 后端 | Python 3.14 · FastAPI · asyncio · httpx · nbtlib · jawa · opencc |
| 前端 | Vue 3 · Vite · pywebview |
| 打包 | PyInstaller · Inno Setup |
| 测试 | pytest（370+ 用例） |

---

## 📁 目录结构

```
Minecraft-translate/
├── backend/                 # FastAPI 后端
│   ├── app/
│   │   ├── main.py          # API / 任务调度 / SSE
│   │   ├── auto_flow.py     # 翻译主流程（阶段化）
│   │   ├── text_sources.py  # 文本源提取
│   │   ├── hardcode.py      # 字节码硬编码 + AI 裁判
│   │   ├── vp.py            # Vault Patcher 补丁
│   │   ├── cfpa.py          # CFPA 词库
│   │   ├── review.py        # AI 质量审查
│   │   ├── detect.py        # 类型识别 + pack_format
│   │   ├── maps/            # 地图汉化
│   │   ├── translate/       # 翻译引擎
│   │   └── data/            # 内置资源
│   └── tests/               # 370+ 测试
├── frontend/                # Vue 3 前端
├── assets/                  # 应用图标
├── scripts/                 # 打包脚本
└── docs/                    # 文档 / 界面预览
```

---

## 🖥 界面预览

查看主界面交互版预览（纸张工坊风格）：**[ui-preview.html](docs/ui-preview.html)**

---

## 🔗 参考的开源项目

本软件研究并参考了以下开源项目（思路借鉴，非代码复用）：

### 直接参考（本地 `_upstream/` 目录，不上传）
| 项目 | 参考内容 |
|------|----------|
| MCC-i18n | 地图文本汉化：世界文件扫描 / 批量编辑 / 一键导出 |
| Minecraft-Mod-Translator | LDC 字节码硬编码提取、技术标识符过滤 |
| mc_translator | Mod 自动翻译（Rust）：多源翻译 |
| XTMC Translate | 前后端模组翻译工具：任务书 / 配置翻译 |

### 功能参考
| 项目 | 参考内容 |
|------|----------|
| Aaalice_Minecraft_Translator | 占位符保护、翻译结果校验、重试队列 |
| mods-string-extractor | `en_us − zh_cn` 差集提取 |
| Translator-Minecraft | KubeJS / FTB Quests 翻译 |
| FTB Quest Localizer | FTB 任务导出语言文件 |
| I18nUpdateMod3 | CFPA 汉化资源包自动下载应用 |
| Vault Patcher | 字节码硬编码运行时替换 |
| Nixinova/pack-format | MC 版本 → pack_format 权威映射 |
| CFPA 汉化包 | 社区人工翻译词库 |

---

## 🛠 构建方式

### 一键打包（Windows）
```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_all.ps1
```
生成：便携版 `dist/便携版/像素译站.exe`、安装版 `dist/安装版/像素译站-Setup.exe`

### 手动构建
```bash
# 1. 后端
cd backend
pip install -r requirements.txt
python -m pytest tests/ -q        # 370+ 用例全绿
uvicorn app.main:app --port 8000  # 开发运行

# 2. 前端
cd frontend
npm install
npm run build                     # 产出 dist/（供打包）

# 3. 打包（需 frontend/dist 存在）
cd ..
pyinstaller scripts\mc_translator.spec
```

### 环境要求
- Python 3.14 · Node.js 18+ · npm
- PyInstaller（打包用）· Inno Setup 6（安装版用）

---

## ⚠️ 免责声明

- 本软件为**个人开发、免费开源**，仅供学习交流，不得用于商业目的。
- 翻译依赖 AI 模型，**质量与所用 API 直接挂钩**（见上），不保证准确性、完整性、一致性，重要内容请自行核对。
- 内置 / 下载的社区词库（CFPA）、第三方 Mod（i18n / VP）版权归各自作者。
- 使用本软件可能生成 / 修改游戏文件，请**提前备份**；造成的 Mod 加载失败、崩溃、存档损坏由使用者自行承担。
- 严禁将本软件用于任何非法用途。

---

## 📄 许可证

开源项目，详见仓库 LICENSE（默认 MIT）。

---

*本 README 由 AI（Claude Code）生成，如有不准确请以实际代码为准。*
