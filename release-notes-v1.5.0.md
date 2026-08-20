## v1.5.0 更新内容

### 🎯 审查/产出提速（核心）
- **审查多 consumer 并行**：原单一审查 consumer 串行（取20条→AI审查几秒→写回→再取），产出 = 20条/审查耗时（用户「token涨但十几条十几条出」根因）。现在多个审查 consumer 并行，产出速度 × 并发数
- **审查 consumer 取消封顶 6**：审查与翻译共享全局 RateGate 令牌桶（admission 管速率），consumer 只是抢令牌的调度单元（execution 管并发）。consumer 数 = 设置并发——翻译管道空下来时令牌立即喂给审查（令牌高效利用，不浪费配额）；令牌桶 acquire() 兜底：consumer 再多也不会超 RPM，绝不触发 API 限流（业界「admission + execution」双层协调模式）
- **术语预扫描后台异步**：翻译前预扫描术语表不再阻塞主翻译（原 await 翻译40个高频词受限流影响卡住「正在翻译语言文件」）
- **审查独立并发信号量**：审查不被翻译占满共享并发池挤占

### ⚡ 令牌协调
- RateGate 桶容量收紧 6 秒配额，防止突发超速限流
- 429 全局协调冷却 + 尊重 Retry-After + 固定模式退档学习

### 📦 打包说明
- 便携版：`release-v1.5.0-portable.exe`（单文件，免安装）
- 安装版：`release-v1.5.0-setup.exe`（需 Inno Setup 6）
