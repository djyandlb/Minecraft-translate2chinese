# 占位文件：create_engine 依赖任务 9（LLMClient）/任务 10（MachineClient）的实现，
# 其运行时断言延后到任务 9/10 完成后补齐。
# 本文件仅作包标记，避免测试收集遗漏。当前仅验证翻译引擎抽象协议可导入。
from app.config import AppConfig
from app.translate.engine import TranslationEngine, create_engine


def test_engine_module_importable():
    assert callable(create_engine)
    assert TranslationEngine is not None


def test_create_engine_llm_follows_provider(tmp_path, monkeypatch):
    # 运行时断言：DEFAULT_CONFIG 预填空值，llm 分支参数必须跟随厂商模板 + smart_defaults
    monkeypatch.setattr("keyring.get_password", lambda *a, **k: "fake-key")
    cfg = AppConfig(tmp_path / "c.json")
    cfg.set("engine", "llm")
    cfg.set("provider", "Kimi")
    cfg.set("concurrency", None)
    cfg.set("batch_size", None)
    cfg.set("llm", {"base_url": "", "model": ""})
    from app.translate.llm import LLMClient
    engine = create_engine(cfg)
    assert isinstance(engine, LLMClient)
    assert engine.base_url == "https://api.moonshot.cn/v1"   # 跟随 Kimi 模板
    assert engine.concurrency == 5                            # 跟随 Kimi 智能默认


def test_create_engine_machine(tmp_path):
    # machine 分支：返回 MachineClient
    cfg = AppConfig(tmp_path / "c.json")
    cfg.set("engine", "machine")
    from app.translate.machine import MachineClient
    assert isinstance(create_engine(cfg), MachineClient)


def test_create_engine_free_follows_free_provider(tmp_path, monkeypatch):
    """free 引擎（第三选项：免费 API）：端点/模型跟随免费平台预设，keyring 取 key。"""
    monkeypatch.setattr("keyring.get_password", lambda *a, **k: "free-key")
    from app.translate.providers import FREE_PROVIDERS
    cfg = AppConfig(tmp_path / "c.json")
    cfg.set("engine", "free")
    cfg.set("provider", "智谱AI")
    cfg.set("concurrency", None)
    cfg.set("batch_size", None)
    cfg.set("llm", {"base_url": "", "model": ""})
    from app.translate.llm import LLMClient
    engine = create_engine(cfg)
    assert isinstance(engine, LLMClient)
    assert engine.base_url == FREE_PROVIDERS["智谱AI"]["base_url"]   # 智谱 GLM-4-Flash 免费端点
    assert engine.model == FREE_PROVIDERS["智谱AI"]["model"]          # glm-4-flash
    assert engine.api_key == "free-key"


def test_create_engine_free_custom_fallback(tmp_path, monkeypatch):
    """free 未知平台回落「自定义免费」（空端点，用户自填）。"""
    monkeypatch.setattr("keyring.get_password", lambda *a, **k: "k")
    cfg = AppConfig(tmp_path / "c.json")
    cfg.set("engine", "free")
    cfg.set("provider", "不存在的免费平台")
    engine = create_engine(cfg)
    assert engine.base_url == ""    # 回落自定义免费：空端点
    assert engine.model == ""


def test_free_providers_fields_complete():
    """免费平台预设字段齐全（base_url/model 必须有，供 LLMClient 构造）。"""
    from app.translate.providers import FREE_PROVIDERS
    for name, d in FREE_PROVIDERS.items():
        assert "base_url" in d and "model" in d, f"{name} 缺 base_url/model"
        assert "concurrency" in d and "batch_size" in d
