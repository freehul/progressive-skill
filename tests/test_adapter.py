"""Hermes adapter tests — plugin load must survive a half-built prompt_builder.

Regression guard for the circular-import case: when Hermes loads the plugin
while ``agent.prompt_builder`` is still initializing, the module object exists
but has no ``build_skills_system_prompt`` attribute yet.  ``register()`` must
still complete so the ``on_session_start`` retry hook gets registered.
"""
import importlib.util
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


class _RecordingAPI:
    """Minimal stand-in for Hermes's PluginAPI."""

    def __init__(self):
        self.hooks = []

    def register_hook(self, name, fn):
        self.hooks.append(name)


def _load_adapter():
    """Import __init__.py fresh, as a package so relative imports resolve."""
    for stale in [n for n in sys.modules if n.startswith("progressive_adapter")]:
        del sys.modules[stale]
    spec = importlib.util.spec_from_file_location(
        "progressive_adapter",
        REPO_ROOT / "__init__.py",
        submodule_search_locations=[str(REPO_ROOT)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["progressive_adapter"] = mod
    spec.loader.exec_module(mod)
    return mod


def _install_bare_prompt_builder():
    """A module with no attributes — what a circular import exposes."""
    bare = types.ModuleType("agent.prompt_builder")
    sys.modules["agent.prompt_builder"] = bare
    return bare


def test_register_survives_partially_initialized_module():
    _install_bare_prompt_builder()
    mod = _load_adapter()

    api = _RecordingAPI()
    mod.register(api)  # must not raise AttributeError

    assert api.hooks == ["on_session_start", "post_tool_call", "on_session_end"]


def test_patch_reports_failure_without_raising():
    _install_bare_prompt_builder()
    mod = _load_adapter()

    assert mod._patch_prompt_builder() is False


def test_session_start_retry_patches_once_module_is_ready():
    bare = _install_bare_prompt_builder()
    mod = _load_adapter()
    mod.register(_RecordingAPI())  # patch deferred, hooks registered

    def real_build(available_tools=None, available_toolsets=None,
                   compact_categories=None, **kwargs):
        return "  demoted-cat [names only]: alpha, beta\n"

    bare.build_skills_system_prompt = real_build

    mod._on_session_start()  # the safety net that was unreachable before

    assert bare.build_skills_system_prompt is not real_build
    out = bare.build_skills_system_prompt(
        available_tools=["read_file"], available_toolsets=["terminal", "web"]
    )
    assert "demoted-cat (2)" in out
    assert "[names only]" not in out
