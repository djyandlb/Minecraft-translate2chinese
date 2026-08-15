# -*- coding: utf-8 -*-
"""汉化产物审查门禁：输出前校验 jar 完整性，防「输出即崩」。

用户反馈：汉化产物直接输出导致部分 mod 启动崩溃（Sodium mixin 结构被重写破坏、
Xaero 用翻译后的 effect 名拼资源位置 Identifier 产生非法字符）。虽然替换阶段已做
class 结构指纹校验、语言文件阶段已保护 Xaero effect key，这里作为**产物落盘前的
最后一道防线**：zip 完整 + mod 元数据可解析 + 语言文件可解析 + 已知 Identifier
风险复查。审查不过 → 不输出该 jar（调用方删除产物并提示），避免交付破坏性产物。
"""
import json
import zipfile
from pathlib import Path

# Minecraft ResourceLocation（Identifier）允许字符：a-z0-9/._-。
# 语言文件值若含非 ASCII 且被 mod 拼 Identifier 会崩溃（xaerominimap:无小地图 实测）。
# 翻译阶段 effect.* 已交 AI 自主判断（会拼 Identifier 的效果名保留英文，纯显示翻译）；
# 这里兜底复查产物，非 ASCII 软提示（AI 判断失误时的最后防线，不拦截——正常翻译的
# 纯显示效果名也会非 ASCII，拦截会让用户拿不到 jar）。


def verify_translated_jar(jar: Path, target_lang: str = "zh_cn") -> dict:
    """审查汉化 jar：zip 完整 + 目标语言文件可解析（硬性失败，不输出）+ 软风险提示。

    返回 {"ok": bool, "issues": list[str], "warnings": list[str]}。ok=False 为硬性
    失败（zip 损坏/语言文件损坏），调用方不应输出；warnings 为软风险（已知风险 mod
    的 effect 值非 ASCII），只提示不拦截——语言文件阶段已保护 Xaero effect 保持英文，
    触发软风险的通常是用户 mods 里已有的旧汉化 jar（复制进 hardcoded），删产物会让
    用户拿不到 jar（实测小地图产物变空「整合包汉化.zip」）。
    """
    issues: list[str] = []      # 硬性失败 → 不输出
    warnings: list[str] = []    # 软风险 → 提示，保留产物
    # 修复（recheck）：语言文件后缀随 pack_format——1.12 及以下（pack_format<4）是 .lang/
    # .properties 而非 .json，原只匹配 *.json 使老版本产物门禁形同虚设
    _LANG_SUFFIXES = (".json", ".lang", ".properties")
    try:
        with zipfile.ZipFile(jar) as zf:
            # 性能修复：testzip() 全量解压测 CRC，100~500MB 汉化 jar 可卡数十秒。
            # 改为：目标语言文件全查 + 其余条目均匀采样（≤20 条）抽查 CRC。
            names = zf.namelist()
            target_lang_names = [n for n in names
                                 if n.endswith(tuple(f"{target_lang}{s}" for s in _LANG_SUFFIXES))]
            stride = max(1, len(names) // 20)
            sampled = [n for i, n in enumerate(names) if i % stride == 0][:20]
            bad = None
            for n in set(target_lang_names) | set(sampled):
                try:
                    zf.read(n)
                except Exception:
                    bad = n
                    break
            if bad is not None:
                issues.append(f"zip 损坏条目：{bad}")
            for n in names:
                if "/lang/" not in n:
                    continue
                # 只检查目标语言文件（我们翻译写入的）；原版自带的其他语言跳过
                if not n.endswith(tuple(f"{target_lang}{s}" for s in _LANG_SUFFIXES)):
                    continue
                try:
                    if n.endswith(".json"):
                        # 修复（recheck）：剥 BOM——utf-8-sig 解码，带 BOM 的 json 不再被
                        # json.loads 误判「语言文件损坏」→ 整 jar 不输出（对齐 scanner/langfile）
                        data = json.loads(zf.read(n).decode("utf-8-sig"))
                    else:
                        from app.langfile import parse_lang, parse_properties
                        txt = zf.read(n).decode("utf-8-sig")
                        data = parse_lang(txt) if n.endswith(".lang") else parse_properties(txt)
                except Exception:
                    # 修复：非 UTF-8（GBK 老汉化 jar）尝试宽松解码，能解析则软提示不误杀产物
                    try:
                        txt = zf.read(n).decode("utf-8-sig", errors="replace")
                        if n.endswith(".json"):
                            data = json.loads(txt)
                        else:
                            from app.langfile import parse_lang, parse_properties
                            data = parse_lang(txt) if n.endswith(".lang") else parse_properties(txt)
                        warnings.append(f"{n}: 非 UTF-8 编码，已宽松解析（若为旧汉化文件可忽略）")
                    except Exception:
                        issues.append(f"语言文件损坏：{n}")
                        continue
                # 通用软提示：任何 mod 的 effect 名都可能被拼 Identifier（Xaero 多次实测），
                # 翻译阶段已统一保持 effect 英文；这里兜底复查产物，非 ASCII 则提示不拦截
                if isinstance(data, dict):
                    for k, v in data.items():
                        if k.startswith("effect.") and isinstance(v, str) and not v.isascii():
                            warnings.append(f"{n}: {k} 含非 ASCII（若被拼 Identifier 可能崩溃）")
    except Exception as e:
        issues.append(f"无法打开 jar：{e}")
    return {"ok": not issues, "issues": issues, "warnings": warnings}
