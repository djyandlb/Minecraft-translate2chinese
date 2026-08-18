## v1.4.2 更新内容

### 🐛 修复大面积翻译失败（优化代码串过滤规则）

用户实测整合包翻译失败条数骤增。经分析发现 `should_translate` 对纯字母代码串的过滤不够——全大写模式串（`PCPPPPPCP`）、驼峰代码标识（`ModelViewMat`、`texCoord`）被当文本送进 AI，翻译出垃圾后审查打回、强制重翻还是坏，最终记 failed。

**修复**：`should_translate` 新增纯字母放行规则：
- **全大写 ≥4 字符**（`PCPPPPPCP`、`HHHHHHHHH`）→ 代码/模式串，跳过
- **驼峰**（小写后跟大写，如 `ModelViewMat`、`texCoord`）→ 代码标识，跳过
- **普通英文单词**（`Bombs`、`Enable`、`Brightness`）→ 用户可见文本，正常翻译

### 🐛 修复终审 failed 逻辑（有中文就不记 failed）

终审 forced 重翻失败后直接记 failed，但初翻可能已经有中文译文（只是审查打回质量差）。

**修复**：终审时如果 forced 重翻没出中文，检查**初翻是否有中文**——有则用初翻的中文译文，不记 failed。宁可要质量差的中文翻译，也不要记 failed 导致没翻译。

### 🐛 修复 Patchouli multiblock.pattern 被翻译

Patchouli 教程书的 `multiblock.pattern` 字段（方块排列代码 `PCP000PCP`）被当文本翻译成「底座哭泣的黑曜石」。

**修复**：`_walk_json` 跳过 `.multiblock.pattern` 字段。

### ⚡ 硬编码阶段多批并发

硬编码 AI 判断原逐批串行（每批等 API 响应再下一批），500 条/batch_size=20 → 25 批串行要几十秒。

**修复**：`asyncio.gather` 同时发多批，受引擎全局并发池控制，不再串行等待。

### ⚡ 语言文件阶段预扫描

开局有 20000 条已汉化时，进度条从 0 慢慢涨到 20000（每秒几百条），用户以为不涨。

**修复**：翻译前预扫描——已汉化/技术串/作者名批量 bump done，进度条开局就涨到位。

### 🐛 修复 Bombs/Plantkillable 等短词不翻译

语言文件里的短词（画作标题 `Bombs`、`Plantkillable`）AI 返回原文后被 `keep_original_ok` 自动放行。

**修复**：语言文件阶段 `keep_original_ok=False`，必须送审查判断。

### 🐛 修复翻译明细换行不显示

语言文件值里的多行文本在翻译明细里显示成一行。

**修复**：`white-space: normal` → `pre-wrap`，真实换行符正确显示。

### 🐛 修复 lang total 虚标（61885 → 实际 36740）

语言文件阶段 total 包含了已汉化/记忆命中/跳过的条目，虚标严重。

**修复**：total 只算待翻译缺口 `len(state_jobs)`；预扫描跳过的条目只推 stage done，不加全局 done。

### 📦 排除无用库缩减体积

系统 Python 环境里的 torch/llvmlite/PyQt5/scipy 等 685MB 无用库被打包进去。

**修复**：spec 的 `excludes` 排除这些库，便携版从 346MB → 70MB，安装版从 249MB → 58MB。

### 🔧 重试逻辑加固

API 请求失败后重试之间没有退避等待——立即重试 → 又失败 → 2 次用完直接记 failed。

**修复**：4 次重试 + 指数退避（1s/2s/4s/8s），给 API 恢复窗口。

---

**安装包**：便携版双击即用；安装版走安装向导。
**测试**：431 自动化测试全绿。
