"""Progressive-disclosure plugin — smart skill index compaction (v3).

v3 architecture (2026-08-05): decision core extracted to ``core/``.
  - ``core/`` is agent-agnostic pure Python (zero Hermes imports); any
    agent can drive the same demotion/budget logic via ProgressiveCore.
  - This module is a thin Hermes adapter: binds Hermes data sources
    (usage.json, skills snapshot, skills dir, plugin.yaml) and hooks.

v2 heritage (still applies):
  - Decision/render separation: the plugin ONLY decides WHICH categories
    get demoted (compact_categories); Hermes's native rendering handles
    the demotion ([names only] lines).  No text post-processing except
    the pattern-locked budget transforms in core/budget.
  - Handles upstream signature drift: if build_skills_system_prompt drops
    the compact_categories kwarg, the wrapper catches TypeError and falls
    back to a plain call (plugin becomes transparent, never breaks).
  - Phase 2 (usage frequency): skill_view / skill_manage calls are
    recorded into usage.json.  Frequently-used categories are promoted
    (NOT demoted) even when no toolset mapping points at them.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from hermes_cli.plugins import PluginAPI

try:
    from .generic.core import ProgressiveCore, UsageTracker
except ImportError:  # 无包上下文（如 pytest 收集根模块时）
    from generic.core import ProgressiveCore, UsageTracker

logger = logging.getLogger(__name__)

_PLUGIN_DIR = Path(__file__).parent
_USAGE_FILE = _PLUGIN_DIR / "usage.json"

_core: Optional[ProgressiveCore] = None
_core_lock = threading.Lock()


def _snapshot_path() -> Path:
    """Cross-platform snapshot path under HERMES_HOME."""
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / ".skills_prompt_snapshot.json"
    except Exception:
        # Fallback: env var, then platform defaults — never hard-code.
        home = os.environ.get("HERMES_HOME")
        if home:
            return Path(home) / ".skills_prompt_snapshot.json"
        return Path.home() / ".hermes" / ".skills_prompt_snapshot.json"


def _skills_dir() -> Optional[Path]:
    try:
        from hermes_constants import get_skills_dir

        return get_skills_dir()
    except Exception:
        return None


def _get_core() -> ProgressiveCore:
    """Lazily build the ProgressiveCore bound to Hermes data sources."""
    global _core
    with _core_lock:
        if _core is not None:
            return _core
        _core = ProgressiveCore(
            tracker=UsageTracker(_USAGE_FILE),
            snapshot_path=_snapshot_path(),
            skills_dir=_skills_dir(),
            yaml_path=_PLUGIN_DIR / "plugin.yaml",
        )
        return _core


# ---------------------------------------------------------------------------
# Monkey-patch: wrap build_skills_system_prompt (decision injection)
# ---------------------------------------------------------------------------

_patch_lock = threading.Lock()
_patched = False


def _patch_prompt_builder() -> bool:
    """Install the v3 wrapper.  Idempotent + thread-safe.  True on success."""
    global _patched
    with _patch_lock:
        if _patched:
            return True

        pb = sys.modules.get("agent.prompt_builder")
        if pb is None:
            try:
                import agent.prompt_builder as pb  # noqa: F811
            except ImportError:
                logger.warning(
                    "progressive-skill: agent.prompt_builder not in "
                    "sys.modules and cannot be imported; will retry later"
                )
                return False

        original = pb.build_skills_system_prompt

        def wrapped_build_skills_system_prompt(
            available_tools=None,
            available_toolsets=None,
            compact_categories=None,
            **kwargs,
        ):
            core = _get_core()
            demote = core.compute_demote(
                available_tools, available_toolsets, compact_categories
            )
            # Decision injection: pass demotion set to native rendering,
            # forwarding every other kwarg (e.g. skills_dir_override) so
            # the wrapper is transparent to upstream signature drift.
            # If upstream dropped the kwarg, fall back to a plain call —
            # the plugin becomes transparent instead of breaking.
            try:
                result = original(
                    available_tools=available_tools,
                    available_toolsets=available_toolsets,
                    compact_categories=demote,
                    **kwargs,
                )
            except TypeError:
                logger.info(
                    "progressive-skill: compact_categories kwarg "
                    "unsupported upstream; falling back to plain call"
                )
                result = original(
                    available_tools=available_tools,
                    available_toolsets=available_toolsets,
                    **kwargs,
                )
            if not result:
                return result

            # Phase 3: budget-driven post-process (count lines + freq cap).
            relevant = core.infer_relevant(available_toolsets, available_tools)
            if relevant:
                return core.apply_budget(result, relevant)
            return result

        pb.build_skills_system_prompt = wrapped_build_skills_system_prompt
        _patched = True

        # Clear in-process LRU cache so the next call rebuilds with the patch
        try:
            pb.clear_skills_system_prompt_cache()
            logger.info("progressive-skill: patched (v3, core-based) ✓")
        except AttributeError:
            logger.info("progressive-skill: patched (v3) ✓ (cache clear N/A)")

        return True


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(api: "PluginAPI") -> None:
    """Called by Hermes when the plugin is loaded."""
    ok = _patch_prompt_builder()
    if ok:
        logger.info("progressive-skill: register() complete ✓")
    else:
        logger.warning(
            "progressive-skill: patch deferred (agent.prompt_builder "
            "not ready); will retry on session start"
        )
    api.register_hook("on_session_start", _on_session_start)
    api.register_hook("post_tool_call", _on_post_tool_call)
    api.register_hook("on_session_end", _on_session_end)


def _on_session_start(**kwargs) -> None:
    """Re-apply patch on session start (safety net for deferred loading)."""
    _patch_prompt_builder()


def _on_post_tool_call(
    tool_name: str = "",
    args: Optional[Dict[str, Any]] = None,
    result: Any = None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    **_: Any,
) -> None:
    """Record skill usage from skill_view / skill_manage calls."""
    if tool_name not in ("skill_view", "skill_manage"):
        return
    if not isinstance(args, dict):
        return
    name = args.get("name") or args.get("skill_name") or ""
    if name:
        _get_core().tracker.record(name)


def _on_session_end(
    session_id: str = "",
    completed: bool = True,
    interrupted: bool = False,
    **_: Any,
) -> None:
    """Flush usage stats to disk at session end."""
    _get_core().tracker.save()
