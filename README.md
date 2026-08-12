# 像素译站 · Pixel Translation Station

Minecraft 整合包 / Mod / 地图 / 光影的 **AI 一键汉化工具**。

拖入文件 → 自动识别 → 翻译 → 生成可用的汉化产物，全程可视化。

免费开源 · Windows 桌面应用。

---

## 立即获取

[GitHub Releases 下载最新版](https://github.com/djyandlb/Minecraft-translate2chinese/releases/latest)

- **便携版** `release-v1.0.1-portable.exe` —— 双击即用，无需安装，随身携带
- **安装版** `release-v1.0.1-setup.exe` —— 安装向导，正式部署

---

## 快速开始

1. **下载并打开应用**（首次打开自动弹出设置）
2. **设置翻译引擎**：选 DeepSeek / 通义 / Kimi / Ollama 等 → 填 API Key → 点「测试连接」
3. **拖入文件**：整合包 / Mod / 地图 / 光影，支持多个，队列逐个翻译
4. **开始翻译** → 完成后点「打开产物文件夹」
5. **放入游戏**：整合包解压到 `resourcepacks`，Mod 替换原 jar，地图 / 光影按其类型放置

> 源语言自动识别，无需手动选择。

---

## 它能做什么

- 整合包 / Mod / 地图 / 光影，全类型一键汉化
- AI 翻译 + CFPA 社区人工词库双通道：词库命中的部分走人工翻译，缺口才走 AI
- 全文本覆盖：语言文件、教程书、任务书、进度、KubeJS 脚本、字节码硬编码
- 三级质量审查：AI 裁判 + 强制重翻 + 目标语言校验，不留纯英文残留
- 断点续联：中断可继续，已翻译不重复
- 自动识别 MC 版本，产物资源包格式正确，游戏不报不兼容

---

## 翻译质量

**翻译质量取决于你配置的 AI 模型。** 模型越强，译文越自然。

| API / 模型 | 质量 |
|-----------|------|
| DeepSeek / 通义 / Kimi 等商业大模型 | 高 |
| 免费 API（GLM-Flash 等） | 中，限量限速 |
| 本地 Ollama 小模型 | 低，视模型而定 |

- 翻译结果不保证 100% 准确，重要内容请自行核对。
- 语言文件中 CFPA 社区词库覆盖的部分使用人工翻译，与 API 无关。

---

## 产物

| 输入 | 产物 |
|------|------|
| 整合包 | `整合包汉化.zip`（资源包 + i18n 模组 + VP 硬编码补丁 + 任务书补丁） |
| 单个 Mod | 汉化 jar |
| 地图 | 汉化存档 |
| 光影 | 汉化光影包 |

---

## 技术栈

Python 3.14 · FastAPI · Vue 3 · Vite · pywebview · PyInstaller · Inno Setup

---

## 开发 / 构建

```bash
# 后端测试
cd backend
python -m pytest tests/ -q

# 前端
cd frontend
npm install && npm run build

# 一键打包（Windows）
powershell -ExecutionPolicy Bypass -File scripts\build_all.ps1
```

生成 `dist/便携版/release-<版本>-portable.exe` 与 `dist/安装版/release-<版本>-setup.exe`。

---

## 免责声明

- 免费开源，仅供学习交流，不得用于商业目的。
- 翻译依赖 AI，质量与所用 API 直接挂钩，不保证准确性与完整性。
- 使用本软件会生成 / 修改游戏文件，请提前备份；造成的损失由使用者自行承担。
- 内置社区词库与第三方 Mod 版权归各自作者。

---

## 许可证

MIT（见仓库 LICENSE）。
