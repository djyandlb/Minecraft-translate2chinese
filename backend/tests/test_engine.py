# 占位文件：create_engine 依赖任务 9（LLMClient）/任务 10（MachineClient）的实现，
# 其运行时断言延后到任务 9/10 完成后补齐。
# 本文件仅作包标记，避免测试收集遗漏。当前仅验证翻译引擎抽象协议可导入。
from app.translate.engine import TranslationEngine, create_engine


def test_engine_module_importable():
    assert callable(create_engine)
    assert TranslationEngine is not None
