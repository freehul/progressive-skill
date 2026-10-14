"""插件加载路径测试 — 覆盖「agent.prompt_builder 部分初始化」场景（issue #3）。

背景：循环导入时 sys.modules 里会存在一个尚未初始化完成的
``agent.prompt_builder`` 模块（非 None，但缺目标属性）。旧代码在取
``build_skills_system_prompt`` 时抛 AttributeError 且未捕获，导致整个
插件注册中止。

正确行为：识别为「尚未就绪」，返回 False 延迟重试
（register 时失败 → on_session_start 重试）。
"""
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_plugin():
    """按文件路径加载插件根模块（模拟无包上下文的加载方式）。"""
    spec = importlib.util.spec_from_file_location(
        "progressive_skill_plugin", ROOT / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def plugin():
    return _load_plugin()


def test_partial_init_defers_without_raising(plugin, monkeypatch):
    """部分初始化（缺 build_skills_system_prompt）→ 返回 False，不抛异常。"""
    partial = ModuleType("agent.prompt_builder")  # 尚无目标属性
    monkeypatch.setitem(sys.modules, "agent.prompt_builder", partial)
    assert plugin._patch_prompt_builder() is False


def test_retry_after_module_ready(plugin, monkeypatch):
    """延迟重试：模块就绪后再调 → 成功打补丁。"""
    fake = ModuleType("agent.prompt_builder")
    monkeypatch.setitem(sys.modules, "agent.prompt_builder", fake)
    assert plugin._patch_prompt_builder() is False
    fake.build_skills_system_prompt = lambda **kwargs: "index"
    fake.clear_skills_system_prompt_cache = lambda: None
    assert plugin._patch_prompt_builder() is True


def test_missing_module_defers(plugin, monkeypatch):
    """模块缺失（无法导入）→ 返回 False，不抛异常。"""
    monkeypatch.setitem(sys.modules, "agent.prompt_builder", None)
    assert plugin._patch_prompt_builder() is False


def test_full_module_patches_and_is_idempotent(plugin, monkeypatch):
    """就绪模块 → 打补丁成功；重复调用幂等。"""
    fake = ModuleType("agent.prompt_builder")
    fake.build_skills_system_prompt = lambda **kwargs: "index"
    fake.clear_skills_system_prompt_cache = lambda: None
    monkeypatch.setitem(sys.modules, "agent.prompt_builder", fake)
    assert plugin._patch_prompt_builder() is True
    assert plugin._patch_prompt_builder() is True
    # 包装器已安装且可调用
    assert callable(fake.build_skills_system_prompt)
