# 上游仓库审计与复用决策

> 本文件是实施时的许可证与复用依据，由子代理读源码核实（2026-08-09）。
> 四个仓库均克隆于 `_upstream/`，仅作研究参考，不纳入主项目代码分发。

## 审计总表

| 仓库 | 许可证 | 形态 | 可复用内容 | 复用方式 |
|---|---|---|---|---|
| [xtmc-minecraft-mod-translator](https://github.com/Mai-xiyu/xtmc-minecraft-mod-translator) | **无 LICENSE 文件**（默认版权保留） | Python(FastAPI) + Vue3 | `ClassFileModifier` 常量池解析/改写算法、`ai_translator.py` 多厂商客户端设计 | **只参考算法，全部自研**（JVM class 格式是公开规范） |
| [zVictorium/Minecraft-Mod-Translator](https://github.com/zVictorium/Minecraft-Mod-Translator) | **CC BY-NC 4.0**（非商业需署名） | Python CLI | `retry_logic.py` 指数退避重试、`parse_json_with_comments`、语言文件读写函数 | 非商用可借鉴思路；商用需重写。保留署名 |
| [BiliBiliACEGE/MCC-i18n](https://github.com/BiliBiliACEGE/MCC-i18n) | **MIT** | Python(PyQt6) + nbtlib | `utils/json_validator.py`、`utils/exceptions.py`、`utils/config.py`、`utils/mock_translator.py`（纯 stdlib 可直接拷贝）；`utils/nbt_helper.py`（依赖 nbtlib）；`scan_worker`/`write_worker` 的 NBT 递归扫描/写回算法 | **可直接拷贝**，保留 MIT 版权声明 |
| [mn12345678910/mc_translator](https://github.com/mn12345678910/mc_translator) | **MIT** | **Rust(Tauri)**，无 Python | `text_processing.rs` 占位符保护、`skip_rules.rs` 跳过滤、批次→半批→单条降级链、术语 Aho-Corasick 自动机 | 转译思想为 Python 实现（MIT 允许） |

## 各仓库关键结论

### xtmc（字节码核心，无许可证 → 自研）
- `backend/main.py:108` `ClassFileModifier`：解析 JVM 常量池（tag: Utf8/String/Class/Fieldref/Methodref…），`parse()` 提取 `CONSTANT_Utf8`，`modify_utf8_strings(modifier_func)` 改写，`build()` 重建，尾部字节原样保留（不改方法体，仅替换字符串内容，长度变化安全）。
- 依赖仅 `struct`，零第三方。**算法必须自研重写**，因为无 LICENSE。
- `backend/ai_translator.py`：AITranslator 基类 + DeepSeek/OpenAI/Claude/Gemini 子类，统一 `async translate_batch(texts, target_lang) -> list[str]`，含 `_should_translate` 技术串过滤规则、失败回原文。依赖仅 `httpx`。
- 已知坑：`/translate/bytecode/preview` 路由是死代码（`process_jar` 无 `preview_mode` 参数）。

### zVictorium（CC BY-NC 4.0 → 非商用借鉴）
- `src/app/utils/retry_logic.py`：`RateLimitTracker` + `retry_with_exponential_backoff` 装饰器 + `TranslationRateLimiter` 全局单例。纯标准库。
- `src/app/commands/translate.py`：`Translator` 类（Google/OpenAI 双通道，`translate_data(data)` 批量分发）、`FileManager`（jar 解包/重打包、`.json`/`.lang`/`.mcfunction` 读写）。
- 语言文件读写要点：`.json` 用去注释解析（`remove_comments_from_json` 正则去 `//`、`/* */`）；`.lang` 按行 `key=value`、`split("=", 1)`；写出 `json.dump(indent=4, ensure_ascii=False)`。

### MCC-i18n（MIT → 可直接拷贝）
- 地图扫描核心在 `workers/scan_worker.py`：`scan_nbt_data` 递归遍历 NBT Compound/List，白名单键 `['Command','CustomName','Name']`；字符串尝试 `json.loads` 后走 `scan_json_text` 递归找 `text`/`extra` 字段。`is_translatable_text(text)` 过滤规则在 `:310`。
- 写回核心 `workers/write_worker.py`：`replace_in_nbt`（String 值内 `str.replace`）、`replace_json_in_nbt`（只改 `text` 键）；写前 `shutil.move` 成 `.bak`。
- **已知局限**：MCA 区块是字节正则 hack（`scan_mca_file` 直接对二进制做 `Command:`/`CustomName:`/`text:`/`name:` 正则）；**告示牌无专门实现**；Boss 栏在 `json_validator.py:333` `process_bossbar_command` 命令级处理。生产级需另用 `anvil`/`python-amulet` 或自研 MCA 解析。
- `utils/json_validator.py` 是纯函数、零第三方依赖，可直接拷为 `backend/app/maps/json_validator.py`（地图里程碑用）。

### mc_translator（MIT → 转译思想，Rust）
- `text_processing.rs:185` `preprocess_text`：正则 `§x/&x/#Hex6/%s/%1$s/{0}/\n` → 替换为 `%%MC_i%%`/`%%HEX_i%%`/`%%VAR_i%%` 标记，`postprocess_text`（`:207`）还原（容忍变体、索引越界剔除幻觉标签）。→ 本计划 `placeholder.py` 按此思想实现。
- `skip_rules.rs` `should_skip_key/should_skip_value`：黑名单键、`*_id`/`id_*`、纯数字、文件名/扩展、命名空间 `a:b`、hex/UUID、ALL_CAPS、snake_case、Base64、日期 → 全部跳过。→ 本计划 `translate/common.py` 过滤规则。
- `batching.rs:147` 降级链：全量批 → 失败半批 → 最终逐条，逐条失败仅记日志。→ 本计划 `llm.py` 实现。
- `glossary/automaton.rs`：Aho-Corasick 词边界术语匹配 + 提示词注入（上限 30 条）。→ 本计划 `glossary.py` 简化实现。
- `api/client.rs` Google Free：`translate.googleapis.com/translate_a/single?client=gtx` + 3 次退避。→ 本计划 `machine.py` 用 `deep-translator` 替代。

## 版权与署名要求

- 拷贝 MCC-i18n 模块时，保留其 MIT 版权声明头。
- 借鉴 zVictorium 思路（CC BY-NC），项目 README 中署名致谢。
- 本项目默认 MIT 许可证，README 注明"参考了 MCC-i18n（MIT）与 mc_translator（MIT），借鉴 zVictorium（CC BY-NC）思路"。
