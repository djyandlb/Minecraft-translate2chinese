import json
import re
import sys
import zipfile
from pathlib import Path
from app.version import pack_format_to_lang_ext

# modid 与 target_lang 均为不可信外部输入：白名单 [a-z0-9_-]，不含 "."，杜绝 ".."、"/" 等路径穿越
_IDENT_RE = re.compile(r"[a-z0-9_-]+")

def pack_mcmeta(pack_format: int, description: str = "MC Auto Translator") -> dict:
    return {"pack": {"pack_format": pack_format, "description": description}}


def _pack_icon_path() -> Path | None:
    """定位资源包图标 pack.png（frozen → _MEIPASS/assets；否则项目根 assets/）。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", "."))
    else:
        base = Path(__file__).resolve().parent.parent.parent
    p = base / "assets" / "pack.png"
    return p if p.exists() else None


def build_resource_pack(translations: dict[str, dict[str, str]], target_lang: str,
                        pack_format: int, out_path: Path) -> None:
    """把 {modid: {key: value}} 生成标准资源包 zip，语言文件后缀随 pack_format。"""
    ext = pack_format_to_lang_ext(pack_format)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("pack.mcmeta", json.dumps(pack_mcmeta(pack_format), ensure_ascii=False, indent=2))
        icon = _pack_icon_path()
        if icon:
            # 资源包图标 pack.png：整合包界面/资源包列表显示
            zf.write(icon, "pack.png")
        # target_lang 不可信：白名单校验失败则跳过全部语言文件写入
        if not _IDENT_RE.fullmatch(target_lang):
            return
        for modid, entries in translations.items():
            # modid 不可信：白名单不含 "."，拦截 ".."、"/" 等路径穿越手段
            if not entries or not _IDENT_RE.fullmatch(modid):
                continue
            zf.writestr(f"assets/{modid}/lang/{target_lang}.{ext}",
                        json.dumps(entries, ensure_ascii=False, indent=2))
