import copy
import json
import os
from pathlib import Path

DEFAULT_CONFIG = {
    "engine": "llm",                       # "llm" | "machine"，互斥
    "provider": "DeepSeek",                # V3：厂商名（DeepSeek/通义千问/Kimi/Ollama/自定义）
    "source_lang": "en_us",
    "target_lang": "zh_cn",
    "llm": {"base_url": "", "model": ""},   # 空 → 由 provider 模板主导，避免遮蔽厂商智能默认
    "machine": {"provider": "google"},
    "concurrency": None,                    # None = 未显式填，走厂商 smart_defaults
    "scan_concurrency": 4,                  # 扫描并发数：同时解压/解析的 mod jar 数（设置页可调）
    "silly_mode": False,                    # 胡言乱语模式：搞笑/热梗翻译但忠实原意（设置页开关）
    "cache_dir": "",                        # 缓存/工作目录（设置页可改；空 = 默认系统 temp/mc-translator）
    "batch_size": None,
    "pack_format": 15,
    "rpm_limit": 0,                        # 0 = 不限
    "calibrated_rpm": 0,                   # v1.2.9：动态测试校准的该 API 建议 RPM，作为
                                           # rate_gate auto 模式的初始目标（避免从 30 爬坡）
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
        # configured 标记：任何一次保存都视为「用户已配置过」，前端据此判断是否弹开屏设置
        # （pywebview 的 localStorage 不持久，改用后端 config.json 持久标记跨启动保留）
        self.data["configured"] = True
        # 修复：原子写（临时文件 + os.replace），写中断/崩溃不损坏 config.json
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        # 守卫：api_key 禁止写入 config，必须走 keyring
        if key == "api_key":
            raise ValueError("api_key 必须走 keyring，禁止写入 config")
        # 修复：嵌套字段（llm 等）整体替换会清空同级其他字段——前端只提交 {llm:{model}}
        # 会把 llm.base_url 丢掉 → 翻译失败。dict 值深合并（保留已有字段）。
        if isinstance(value, dict) and isinstance(self.data.get(key), dict):
            self.data[key] = {**self.data[key], **value}
        else:
            self.data[key] = value
