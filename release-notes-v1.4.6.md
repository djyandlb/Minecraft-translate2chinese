## v1.4.6 更新内容

### 🐛 Bug 修复
- **翻译极慢问题**：RPM下限从30提升到50，照顾低端API
- **动态测试RPM丢失**：无论rpm_auto是否为True，都保存calibrated_rpm（动态测试值不会丢失）
- **攒批阈值过大**：攒够batch_size就触发翻译，不需要攒够batch_size*concurrency

### ⚡ 性能优化
- **RPM限流优化**：桶容量更大，允许突发请求，不会被限流器卡住
- **重试逻辑优化**：区分错误类型，限流等更久（10-60秒），超时等更短（2-30秒）
- **动态批大小**：跟随RPM自动调整，充分利用并发

### 📦 打包说明
- 便携版：`release-v1.4.6-portable.exe`（单文件，免安装）
- 安装版：`release-v1.4.6-setup.exe`（需 Inno Setup 6）
