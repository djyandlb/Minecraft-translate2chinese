## v1.3.9 更新内容

### 🐛 修复「FTB 任务 / KubeJS 没全量翻译」（用户实测：ftbquestlocalizer 3639 key 全英文残留）

**根因**：FTB Quests 的 `ftbquestlocalizer` 这类 mod **只有 zh_cn 语言文件、没有 en_us**——它把英文直接写进 zh_cn 做占位（`ftbquests.chapter.allthemodium.quest18F3B6750 => ATM Alloy tools`）。扫描器语言文件**只认 en_us 源**、整合包目录文本源也**只收 en_us**（防破坏 mod 自带多语言）→ 这种「只有 zh_cn 英文占位」的 mod **完全漏翻**，产物原样英文。

- **修复**：整合包目录 `assets/*/lang/` **全收**（不再只收 en_us）——只有 zh_cn 英文占位的 mod 现在会被收集翻译
- **值级过滤**：非 en_us 语言文件的值，**含目标语言字符（已汉化）跳过**、纯英文（占位）收集翻译——正常已汉化 mod 不被覆盖，只有英文占位的被补翻
- **写回**：target_path 仍是原 zh_cn 文件（覆盖翻译），FTB 任务显示语言文件值 → 游戏内生效

### 🐛 修复「AE2 教程 markdown / 含路径样文本翻译出原文」（用户实测：教程 line31/35 英文残留）

**根因**：`should_translate` 把 markdown 标题（`# Wireless Terminal`）、链接（`[network's storage](url)`）、含 `/` 的教程正文当**技术串跳过**（首字符 `#`/`[`/含路径）→ AI 没收到 → 原文直接写回。

- **修复**：markdown 标题（`# 空格`/`## 空格` 多级）、链接 `[text](url)`、含空格的教程正文**放行翻译**；只跳真正的技术串（`#version` shader 指令、`/give` 命令、`[i0]` 下标、纯路径）

### 🐛 修复「终审 reason 误判合理保留 → 原文污染记忆」（用户实测：FTB 描述翻译出原文）

**根因**：`_final_judge_batch` 里 `_is_legit_keep_by_source(原文) or _is_legit_keep(reason)`——AI 审查 reason 含「资源路径/代码标识」词就单独放行「合理保留」，即使原文规则说「该翻」（长文本描述含 `Fusion Casing/Glass` 路径样）→ 原文写进记忆污染，后续同文本永久原文。

- **修复**：合理保留必须**原文规则为主**（`_is_legit_keep_by_source` 判技术串才放行）；原文规则说「该翻」的长文本，即使 reason 含路径词也**进二次重试**（forced 再翻一次），不污染记忆；短技术串（≤40 字符）+ reason 双确认才判保留

### 🐛 修复「总进度 80% 与阶段不一致」（用户实测：总 17552/21969，硬编码 7391/8208）

**根因**：跳过/已汉化/CFPA/记忆命中的条目**没算进 done**（总进度不含跳过），但用户要求「总进度必须 100%，只有覆盖率才排除跳过」。

- **修复**：任务完成时**各阶段 done 补足到 total**（跳过算进度）；**覆盖率仍用 `done - skipped`**（跳过排除，report.py 已按 `_skipped_n` 扣）；失败条目不补（`done + failed` 恒 ≤ total）

### 验证

- 后端 **427 测试全绿**（新增：FTB 只 zh_cn 占位收集、markdown 教程翻译、reason 不误判、总进度补足）
- 端到端：`ftbquestlocalizer/lang/zh_cn.json` 英文占位值确认被收集翻译，已汉化值跳过

**安装包**：便携版双击即用；安装版走安装向导。
