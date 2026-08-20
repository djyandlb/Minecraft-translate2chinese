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


class AutoRequest(BaseModel):
    """统一全自动翻译：拖入整合包/mod jar/地图 → 自动识别 → 语言文件+硬编码并入 → 资源包+汉化 jar。

    source_lang 可选：留空走自动检测；用户手动指定时用它（识别失败兜底）。
    selected_hardcoded：B 阶段已弃用（硬编码改 AI 自动判断），保留字段兼容旧请求体，后端不再读取。
    """
    path: str
    target_lang: str = "zh_cn"
    source_lang: str | None = None
    selected_hardcoded: list[str] | None = None   # 已弃用：B 阶段改 AI 自动判断，保留字段兼容
