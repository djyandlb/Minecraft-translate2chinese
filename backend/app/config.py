import copy
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
        # 兼容字符串入参
        self.path = Path(path)
        # 深拷贝默认值，避免多实例共享嵌套 dict
        self.data = copy.deepcopy(DEFAULT_CONFIG)
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, ValueError):
                # 损坏/空文件：备份为 .bak 后回退默认
                self.path.replace(self.path.with_suffix(self.path.suffix + ".bak"))
            else:
                # 顶层必须是 dict，否则回退默认
                if isinstance(loaded, dict):
                    self.data.update(loaded)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 兜底：api_key 绝不落盘
        self.data.pop("api_key", None)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        # 守卫：api_key 禁止写入 config，必须走 keyring
        if key == "api_key":
            raise ValueError("api_key 必须走 keyring，禁止写入 config")
        self.data[key] = value
