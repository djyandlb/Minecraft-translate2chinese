# 地图 .mca 区块文本写回实施计划（待当前批次完成后实施）

> 状态：**已规划，未实施**。前置批次：42 整合包全包覆盖、40 拖放+下载保存、OUTPUTS 移 temp、回归打包。

## Context（背景）

用户确认地图汉化「除图片外全覆盖」的缺口在 **`.mca` 区块文本写回**：
- 现状：`maps/scan.py` 的 `scan_mca` 用 anvil 库 **读** region 区块，能扫出命令方块/区块内告示牌文本
- 但 `maps/write.py` 的 `write_translations` 只支持 `.dat/.json/.mcfunction`；`.mca` 写回未实现
- `maps/flow.py:39-49` 翻译前按 `write_supported` 过滤掉 `.mca` 词条 → **命令方块文本永远汉化不了**

## 技术要点（.mca region 格式）

- region 文件：4KB 对齐 sector
- 文件头 8KB：前 4096B 偏移表（1024 × 4B：3B sector 偏移 + 1B 区块大小）、后 4096B 时间戳表
- 每个区块：4B 长度 + 1B 压缩类型（1=gzip、2=zlib、3=none）+ 压缩 NBT
- 区块 NBT：≤1.14 是 `Level` 包裹，1.15+ 平铺（`scan_mca` 已兼容两种）

## 写回方案（整个 region 重写，最可靠）

对含目标文本的 region 文件 `r.x.z.mca`：

```
1. 读偏移表，按 sector 偏移定位并解压所有区块（zlib/gzip，anvil-parser 或手动）
2. 对目标区块：nbtlib 载入区块 NBT → 按 scan 记录的 chunk(x,z)+nbt_path 定位替换 String → 序列化
3. 重新压缩全部区块（统一 zlib level 6）
4. 重建 region：重新布局 sector（4KB 对齐、紧凑排列）→ 重建偏移表 + 时间戳表 → 写回文件
```

**为什么整 region 重写**：区块压缩后大小变化会破坏后续 sector 偏移，原地覆盖只适用"变小的区块"；整 region 重写（保持 chunk 顺序）100% 可靠，代价是 region 稍大（区块按新大小紧凑排列）。

## 与现有流程整合

- `maps/write.py` 加 `write_mca(file, translations)`：`write_translations` 的 suffix 分支加 `.mca`
- `maps/flow.py`：`write_supported` 加入 `.mca`，移除翻译前对 .mca 的过滤与 warn；`mca_skipped` 提示改为主流程
- `maps/scan.py` 的 `scan_mca` 输出已有 `chunk(x,z)` 前缀路径（`chunk(0,0)Data.Command`）——`write_mca` 按此前缀定位区块，复用现有 nbt_path 替换逻辑
- `main.py` 的 `/api/map-scan` 的 `mca_skipped` 计数调整（不再跳过）

## 测试

- `test_maps_write.py`：`write_mca`——用 anvil-parser **构造一个 region**（含命令方块 NBT）→ 写回 → 重读断言文本替换；区块结构（其他区块/偏移表）不被破坏
- `test_maps_flow.py`：flow 不再跳过 .mca，命令方块文本进入翻译并写回；`.mca` 写回后重读验证
- 真实世界存档冒烟（可选）：含命令方块的存档汉化 → 游戏内验证

## 任务分解

1. **M-mca-1**：`write_mca` 实现（region 读写/重写 + nbtlib 替换）+ 测试（anvil 造 region）
2. **M-mca-2**：`write_translations` 加 .mca 分支 + `flow.py` 移除过滤 + `main.py` mca_skipped 调整 + 测试
3. 回归 + 重新打包 + recheck

## 风险

- anvil-parser 是否支持 region **写**（查库 API；若只读，手动实现 region 偏移表读写）
- 大 region（几十 MB）整重写耗时——可接受（地图汉化一次性操作）
- 压缩类型兼容（老档 gzip / 新档 zlib）

## 完成定义

- `.mca` 区块命令方块/告示牌文本可写回（region 重写可靠，其他区块不受影响）
- 地图汉化「除图片外全覆盖」达成；mca_skipped 提示移除
- 全量测试无回归，已提交
