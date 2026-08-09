import json
from pathlib import Path

DEFAULT_CONFIG = {
    "engine": "llm",                       # "llm" | "machine"，互斥
    "provider": "DeepSeek",                # V3：厂商名（DeepSeek/通义千问/Kimi/Ollama/自定义）
    "source_lang": "en_us",
    "target_lang": "zh_cn",
    "llm": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "machine": {"provider": "google"},
    "concurrency": 8,                      # 可被厂商 smart_defaults 覆盖
    "batch_size": 20,
    "pack_format": 15,
    "rpm_limit": 0,                        # 0 = 不限
    "api_key_ref": "mc-translator",        # keyring 服务名：key 走系统 keyring，绝不落盘
}

class AppConfig:
    """应用配置：json 文件读写，get/set。api_key 不在本文件存储（走 keyring）。"""
    def __init__(self, path: Path):
        self.path = path
        self.data = dict(DEFAULT_CONFIG)
        if path.exists():
            self.data.update(json.loads(path.read_text(encoding="utf-8")))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value
