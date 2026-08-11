"""core — agent-agnostic decision core for progressive-skill.

Pure Python, zero agent imports.  Any agent (Hermes, Claude Code, Codex,
...) can drive the same demotion/budget decisions by binding its own data
sources via :class:`ProgressiveCore`.

Public surface:
    - config            — tunables (decay, promote threshold, budget, ...)
    - scorer.UsageTracker — thread-safe usage-frequency store (usage.json)
    - catalog           — category discovery from snapshot JSON / skills dir
    - selector          — demotion decision logic
    - budget            — budget-driven rendering post-process
    - ProgressiveCore   — facade: bind data sources, drive decisions
"""
from .facade import ProgressiveCore
from .scorer import UsageTracker

__all__ = ["ProgressiveCore", "UsageTracker"]
