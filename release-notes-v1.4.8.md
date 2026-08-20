## v1.4.8 更新内容

### 🎯 令牌协调优化（核心：防止限流/超时/降速）
- **RateGate 桶容量收紧到 6 秒配额**：原桶容量=并发数(64)导致突发64请求超速触发 API 限流（RPM 测了也白测）。现在 cap = min(并发, RPM/10)，请求速率稳定 ≤ RPM
- **429 全局协调冷却**（业界共享协调器模式）：任一请求撞 429 → 所有请求暂停冷却窗口，冷却后慢启动，不再各自退避重试风暴
- **尊重 Retry-After 头**：撞 429 时读取 API 的 Retry-After 建议，按建议冷却
- **固定模式也退档学习**：测出的 RPM 偏高时，撞 429 自动退档到真实配额附近
- **token 落盘节流 500ms**：worker pool 高并发下 token 实时显示，不因 SSE 洪泛被丢

### ⚡ 翻译性能（worker pool 补位）
- 语言文件翻译：攒够 batch_size 就提交任务，并发信号量补位，不再一批等一批
- 硬编码 AI 判断：合并所有 jar 候选批量判断，不再逐 jar 串行排队

### 🗑️ 清理冗余（-521行）
- 删除旧 translator.py、独立 hardcode_flow.py、弃置端点、废弃模型
- 前端统一走 /api/auto-translate 全功能流程

### 📦 打包说明
- 便携版：`release-v1.4.8-portable.exe`（单文件，免安装）
- 安装版：`release-v1.4.8-setup.exe`（需 Inno Setup 6）
