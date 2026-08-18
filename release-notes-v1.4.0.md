## v1.4.0 更新内容

本版共三个修复批次：401 根因、markdown 结构破坏、翻译略过/续联/扫描文案。

---

### 🐛 批 1：修复「每次打开都是 API Key 无效（HTTP 401）」

用户实测：stepfun 计划 API key 之前测试连接成功，最近每次点「测试连接」都报
「API Key 无效或无权限（HTTP 401）：Incorrect API key provided」。

**根因（应用 bug）**：测试连接/动态吞吐验证通过后，只保存了 config（base_url/model），
**从不把验证通过的 api_key 写入系统 keyring**——只有点「保存并关闭」按钮才落盘。
于是：用户填新 key → 测试连接成功 → 直接关窗/切走 → 新 key 丢失 → 下次打开
keyring 退回旧 key → 401。且 401 后输入框仍显示占位符「已配置（••••）」，用户
误以为 key 没问题，反复点测试都失败。

**修复**：
1. **测试连接成功 → 立即把表单中的真实 key 写入 keyring**（动态测试吞吐同理），
   不再依赖用户必须点「保存」按钮
2. **401/403 时自动清掉占位符** → 输入框变空，明确引导用户重新输入 key，
   不再被「已配置」占位符误导
3. 后端实测用户 stepfun key 返回 HTTP 200，确认 key 本身有效——问题在应用读到了
   keyring 里的旧 key

---

### 🐛 批 2：修复 AE2 指南翻译失败骤增 + 链接/表格/分隔线被破坏

用户实测（recheck）：低配模型（stepfun step-3.7-flash）翻译时 failed 条数骤增，
产物出现 `[Subnetworksl../ae2-...` 链接碎片、一页横线、英文没翻好。

**根因**：
1. **采集端把 markdown 结构当内容收走**：AE2 指南 md 的**纯导航链接行**
   （`[Subnetworks](../ae2-mechanics/subnetworks.md)`）、**表格行**（`| Item | Cost |`）、
   **YAML/正文分隔线**（`---`）、**引用定义**（`[ref]: url`）——全被逐行收进翻译管线。
   这些是 markdown **结构**不是内容，低配模型逐个翻译必然破坏 → failed 骤增
2. **placeholder 漏保护链接结构**：只保护了 URL 的 `/路径.md` 段，`](`、`..`、`.md)`
   全裸露 → 模型把 `](` 输出成 `l` → `[Subnetworksl` 碎片

**修复**：
1. 采集端新增 `_is_md_structural_line()`：纯导航链接/表格/分隔线/引用定义**整行跳过**
   （正文段落仍正常收集翻译）
2. placeholder 新增 **markdown 链接整体 token 化**：`[text](url)` 保护为一个标记，
   AI 动结构（`](` 变 l、URL 截断、吃尾括号）→ validate 判失败 → 降级单条重翻
   （注意：只保护 `](url)` 不够——AI 会把 `[text]` 补全成双 `]`，结构照样坏，
   已实测并选择整链接保护）

**实测验证**（stepfun step-3.7-flash 真实 API）：带 `%%MC_n%%` 占位符并明确保留指令时，
低配模型完美保留 token，前后正文正常翻译；动 token 即被 validate 拦截。

---

### 🐛 批 3：断点续联误显示 + 扫描文案不分类型 + 光影前两条略过

#### ① 已产出物的项目不再显示「可断点续联」
单 mod 输出产物后任务行仍显示续联按钮。根因：`_check_resume` 的完成判定用
`done >= total`，但它被「旧任务最大 done」叠加逻辑污染——progress 文件明明写了
`status: "done"` 却不读，已完成项目被旧任务数据误判成「未完成」。

**修复**：改用 progress 文件的 `status` 字段（与左侧项目列表同一标准）——
`status == "done"`（产物已生成）直接判不可续联；旧任务叠加排除已完成任务。

#### ② 扫描状态文案按类型区分
无论源文件什么类型都显示「扫描整合包」。根因：聚 jar 前状态文案在 kind 判断**之前**
执行，所有类型统一写死「整合包」。

**修复**：modpack「扫描整合包」/ modjar「扫描 mod」/ map「扫描地图」/ shader「扫描光影」，
进度 key 同步按类型区分。

#### ③ 光影翻译前两条被略过
`option.TransparentReflections.comment` / `option.WaterReflection.comment` 的值
（`See block.properties to adjust reflective blocks.`）直接跳过不翻译。根因：
`needs_lang_value_translation` 的「带点无空格=技术串」误杀——句中 `.properties` 命中
「点后字母」、句末句号后无空格（无豁免）→ 整句被当非文本跳过。

**修复**：只有**整串无空格**才算技术串（`com.example.Mod`/`path.to.x` 仍跳过）；
含空格的句子即使内嵌路径引用也是用户可见文本，正常翻译。

---

**安装包**：便携版双击即用；安装版走安装向导。
**测试**：431 自动化测试全绿。