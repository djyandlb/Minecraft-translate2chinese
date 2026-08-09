# FastAPI 请求模型（任务 13）：扫描与翻译请求体，目标语言参数化
from pydantic import BaseModel


class ScanRequest(BaseModel):
    path: str
    mode: str = "modpack"          # "modpack" | "jar"
    scope: str = "mods"            # 目录模式下："mods" | "all"
    source_lang: str = "en_us"
    target_lang: str = "zh_cn"


class TranslateRequest(BaseModel):
    path: str
    mode: str = "modpack"
    source_lang: str = "en_us"
    target_lang: str = "zh_cn"
    pack_format: int | None = None
