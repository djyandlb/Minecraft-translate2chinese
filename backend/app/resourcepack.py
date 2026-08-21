import json
import re
import shutil
import sys
import zipfile
from pathlib import Path
from app.version import pack_format_to_lang_ext

# modid 与 target_lang 均为不可信外部输入：白名单 [A-Za-z0-9_-]（允许大写——修复 recheck：
# 大写 modid 之前被全小写正则跳过 → 该 mod 资源包产物缺失，与 text_sources._MODID_RE 不一致），
# 不含 "."，杜绝 ".."、"/" 等路径穿越
_IDENT_RE = re.compile(r"[A-Za-z0-9_-]+")

def pack_mcmeta(pack_format: int | list[int], description: str = "MC Auto Translator") -> dict:
    """pack.mcmeta 的 pack 对象。

    ≤1.21.8 写整数 pack_format；1.21.9+（25w31a 起）pack_format/supported_formats 字段
    弃用，改用 min_format/max_format 数组（min_format 是目标格式，max_format 上限用
    最大 minor 表示「仅该 major」——写 float 会报「不兼容」，整数/数组才对）。
    """
    if isinstance(pack_format, list):
        major = pack_format[0]
        minor = pack_format[1] if len(pack_format) > 1 else 0
        return {"pack": {"min_format": [major, minor],
                         "max_format": [major, 2147483647],
                         "description": description}}
    return {"pack": {"pack_format": pack_format, "description": description}}


def _pack_icon_path() -> Path | None:
    """定位资源包图标 pack.png。

    frozen → **多候选路径**（v1.5.0 修复：用户 13:09 产物资源包无图标——frozen 单查
    `_MEIPASS/assets`，但 PyInstaller 不同版本 onedir 的 `_MEIPASS` 可能指向 exe 目录
    而非 `_internal`（datas 实际在 `_internal/assets`）→ 返回 None → 图标不复制 →
    游戏显示默认图标）；开发 → 项目根 assets/。多候选命中即返回。
    """
    cands: list[Path] = []
    if getattr(sys, "frozen", False):
        _m = Path(getattr(sys, "_MEIPASS", "") or ".")
        exe_dir = Path(sys.executable).resolve().parent
        cands = [
            _m / "assets" / "pack.png",                   # onefile 临时解压 / onedir _internal
            exe_dir / "_internal" / "assets" / "pack.png",  # onedir（_MEIPASS 指向 exe 目录时）
            exe_dir / "assets" / "pack.png",              # 便携版 exe 旁
        ]
    else:
        cands = [Path(__file__).resolve().parent.parent.parent / "assets" / "pack.png"]
    for p in cands:
        if p.exists():
            return p
    return None


def _ext_of(pack_format: int | list[int]) -> str:
    """pack_format（整数或 1.21.9+ 数组）→ 语言文件后缀（.json/.lang）。"""
    return pack_format_to_lang_ext(pack_format if isinstance(pack_format, int) else pack_format[0])


def build_resource_pack(translations: dict[str, dict[str, str]], target_lang: str,
                        pack_format: int | list[int], out_path: Path, description: str = "MC Auto Translator") -> None:
    """把 {modid: {key: value}} 生成标准资源包 zip，语言文件后缀随 pack_format。
    description：pack.mcmeta 的资源包描述（游戏内资源包列表显示）。"""
    ext = _ext_of(pack_format)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("pack.mcmeta", json.dumps(pack_mcmeta(pack_format, description),
                                              ensure_ascii=False, indent=2))
        icon = _pack_icon_path()
        if icon:
            # 资源包图标 pack.png：整合包界面/资源包列表显示
            zf.write(icon, "pack.png")
        # target_lang 不可信：白名单校验失败则跳过全部语言文件写入（防御，不抛不崩；
        # target_lang 为内部生成，正常不触发；测试锁定此行为）
        if not _IDENT_RE.fullmatch(target_lang):
            return
        for modid, entries in translations.items():
            # modid 不可信：白名单不含 "."，拦截 ".."、"/" 等路径穿越手段
            if not entries or not _IDENT_RE.fullmatch(modid):
                continue
            zf.writestr(f"assets/{modid}/lang/{target_lang}.{ext}",
                        json.dumps(entries, ensure_ascii=False, indent=2))


def build_resource_pack_dir(translations: dict[str, dict[str, str]], target_lang: str,
                            pack_format: int | list[int], out_dir: Path, description: str = "MC Auto Translator") -> None:
    """把 {modid: {key: value}} 生成**解压目录结构**的资源包（用户刚需：整合包产物
    解压即用，resourcepacks/模组汉化资源包/ 直接放进游戏 resourcepacks 目录）。
    内容与 build_resource_pack 一致（pack.mcmeta + assets/<modid>/lang/<target>.<ext>）。
    description：pack.mcmeta 的资源包描述（游戏内资源包列表显示）。"""
    ext = _ext_of(pack_format)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "pack.mcmeta").write_text(
        json.dumps(pack_mcmeta(pack_format, description), ensure_ascii=False, indent=2), encoding="utf-8")
    icon = _pack_icon_path()
    if icon:
        try:
            shutil.copy2(icon, out_dir / "pack.png")
        except OSError:
            pass
    if not _IDENT_RE.fullmatch(target_lang):
        return
    for modid, entries in translations.items():
        if not entries or not _IDENT_RE.fullmatch(modid):
            continue
        f = out_dir / "assets" / modid / "lang" / f"{target_lang}.{ext}"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
