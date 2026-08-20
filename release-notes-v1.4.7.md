## v1.4.7 更新内容

### ⚡ 翻译性能大幅提升（并行补位机制）
- **语言文件翻译改为 worker pool 补位**：攒够 batch_size 就提交翻译任务，并发信号量控制同时运行的请求数（动态测试测出的并发），前面完成槽位释放、后面排队自动补位——不再一批等一批串行
- **硬编码 AI 判断合并批量**：所有 jar 的候选一次批量判断（不再逐 jar 串行排队），内部批次并行 + 并发补位

### 🗑️ 清理冗余（节省体积）
- 删除旧版 `translator.py`（死代码，前端已不用 `/api/translate`）
- 删除独立 `hardcode_flow.py` 及弃置端点 `/api/translate`、`/api/hardcode-scan`、`/api/hardcode-translate`
- 删除废弃模型 `HardcodeRequest`、`TranslateRequest`
- 前端统一走 `/api/auto-translate` 全功能流程（含硬编码判断）

### 🐛 Bug 修复
- 并发压力测试：64 档同时 gather 打 API 互相挤占 → 分波并行（每波 8 档）
- 卡在「正在翻译语言文件」：术语预扫描同步阻塞 → 后台线程 + 超时
- API Key 无效不提示：401/403 fatal 被吞 → 捕获返回 fatal meta
- 硬编码翻译静默失败：不传 meta 无法区分失败 → 传 meta 精确计 failed
- 硬编码引擎连接池泄漏 → finally 补 aclose
- 断点续联指纹不一致 → 对齐 unwrap_bare_wrapper
- onUnmounted 覆盖配置 → 加 loaded 守卫
- 前端吞吐测试超时 180s→300s

### 📦 打包说明
- 便携版：`release-v1.4.7-portable.exe`（单文件，免安装）
- 安装版：`release-v1.4.7-setup.exe`（需 Inno Setup 6）
