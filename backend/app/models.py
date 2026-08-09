# FastAPI 请求模型（任务 13）：扫描与翻译请求体，目标语言参数化
from pydantic import BaseModel


class DetectRequest(BaseModel):
    """自动识别请求：只给路径与目标语言，类型/源语言/pack_format 全自动推断。"""
    path: str
    target_lang: str = "zh_cn"


class ScanRequest(BaseModel):
    path: str
    mode: str = "modpack"          # "modpack" | "jar"
    scope: str = "mods"            # 目录模式下："mods" | "all"
    source_lang: str = "en_us"
    target_lang: str = "zh_cn"


class TranslateRequest(BaseModel):
    path: str
    mode: str = "modpack"
    scope: str = "mods"            # 目录模式下："mods" | "all"，贯通扫描时的范围选择
    source_lang: str = "en_us"
    target_lang: str = "zh_cn"
    pack_format: int | None = None
    mc_version: str | None = None   # 未显式 pack_format 时按此版本换算


class MapScanRequest(BaseModel):
    """地图汉化：扫描世界存档副本的可翻译词条。"""
    path: str
    source_lang: str = "en_us"
    target_lang: str = "zh_cn"


class MapTranslateRequest(BaseModel):
    """地图汉化：对世界存档副本发起后台翻译。"""
    path: str
    source_lang: str = "en_us"
    target_lang: str = "zh_cn"


class HardcodeRequest(BaseModel):
    """硬编码汉化：对 mod jar 内 JVM 字节码硬编码字符串发起后台翻译。原 jar 只读。"""
    path: str
    source_lang: str = "en_us"
    target_lang: str = "zh_cn"


class AutoRequest(BaseModel):
    """统一全自动翻译：拖入整合包/mod jar/地图 → 自动识别 → 语言文件+硬编码并入 → 资源包+汉化 jar。

    source_lang 可选：留空走自动检测；用户手动指定时用它（识别失败兜底）。
    """
    path: str
    target_lang: str = "zh_cn"
    source_lang: str | None = None
