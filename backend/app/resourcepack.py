import json
import zipfile
from pathlib import Path
from app.version import pack_format_to_lang_ext

def pack_mcmeta(pack_format: int, description: str = "MC Auto Translator") -> dict:
    return {"pack": {"pack_format": pack_format, "description": description}}

def build_resource_pack(translations: dict[str, dict[str, str]], target_lang: str,
                        pack_format: int, out_path: Path) -> None:
    """把 {modid: {key: value}} 生成标准资源包 zip，语言文件后缀随 pack_format。"""
    ext = pack_format_to_lang_ext(pack_format)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("pack.mcmeta", json.dumps(pack_mcmeta(pack_format), ensure_ascii=False, indent=2))
        for modid, entries in translations.items():
            if not entries:
                continue
            zf.writestr(f"assets/{modid}/lang/{target_lang}.{ext}",
                        json.dumps(entries, ensure_ascii=False, indent=2))
