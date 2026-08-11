"""Demotion decision — which top-level categories to demote (agent-agnostic).

All data (cfg accessor, tracker, category data) is passed in; no globals
beyond the static mapping tables.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, FrozenSet, Optional, Set, Tuple

# Only STRONG links survive. Weak/ambiguous links are intentionally absent;
# their categories are demoted by default and can be promoted later by the
# usage-frequency scorer.
TOOLSET_CATEGORY_MAP: Dict[str, Tuple[str, ...]] = {
    "terminal": ("software-development", "devops", "github", "hermes"),
    "file": ("software-development", "hermes"),
    "web": ("research", "hermes"),
    "browser": ("research", "computer-use"),
    "skills": ("hermes",),
    "memory": ("note-taking",),
}

TOOL_TO_TOOLSET: Dict[str, str] = {
    "terminal": "terminal",
    "process": "terminal",
    "read_file": "file",
    "write_file": "file",
    "patch": "file",
    "search_files": "file",
    "web_search": "web",
    "web_extract": "web",
    "firecrawl": "web",
    "browser_navigate": "browser",
    "browser_click": "browser",
    "browser_snapshot": "browser",
    "computer_use": "browser",
    "skill_view": "skills",
    "skills_list": "skills",
    "skill_manage": "skills",
    "memory": "memory",
    "session_search": "memory",
}


def infer_relevant_categories(
    cfg_get: Callable[[str], Any],
    toolsets: "Optional[Set[str]]" = None,
    tools: "Optional[Set[str]]" = None,
) -> FrozenSet[str]:
    """Return the set of top-level category prefixes relevant to the
    active toolset.  Everything else is a demotion candidate."""
    if not toolsets and not tools:
        return frozenset()  # no info → demote nothing (safety net)

    relevant: Set[str] = set(cfg_get("always_relevant"))

    for ts in toolsets or set():
        relevant.update(TOOLSET_CATEGORY_MAP.get(ts, ()))

    for t in tools or set():
        ts = TOOL_TO_TOOLSET.get(t)
        if ts:
            relevant.update(TOOLSET_CATEGORY_MAP.get(ts, ()))

    return frozenset(relevant)


def frequent_categories(
    cfg_get: Callable[[str], Any],
    usage: Dict[str, Dict[str, Any]],
    skill_map: Dict[str, str],
    decayed_score: Callable[[int, float], float],
) -> FrozenSet[str]:
    """Return top-level categories promoted by usage frequency.

    A category is promoted when ANY of its skills has a decayed score
    ≥ promote_score (configurable).  Promoted categories are NOT demoted
    even when no toolset mapping points at them.
    """
    if not usage:
        return frozenset()

    threshold = float(cfg_get("promote_score"))
    promoted: Set[str] = set()
    for skill_name, entry in usage.items():
        if not isinstance(entry, dict):
            continue
        try:
            score = decayed_score(
                int(entry.get("count", 0)),
                float(entry.get("last_used", 0) or 0),
            )
        except (TypeError, ValueError):
            continue
        # Epsilon tolerance: exp() float noise (e.g. 1.999999998)
        # must not block an exact-threshold promotion.
        if score >= threshold - 1e-6:
            cat = skill_map.get(skill_name)
            if cat:
                promoted.add(cat)
    return frozenset(promoted)


def compute_demote_set(
    cfg_get: Callable[[str], Any],
    all_cats: Set[str],
    skill_map: Dict[str, str],
    usage: Dict[str, Dict[str, Any]],
    decayed_score: Callable[[int, float], float],
    available_tools: "Optional[Set[str]]" = None,
    available_toolsets: "Optional[Set[str]]" = None,
    existing_compact: "Optional[FrozenSet[str]]" = None,
) -> "FrozenSet[str]":
    """Decide which top-level categories to demote.

    Merge: user-configured compact categories + toolset-inferred
    non-relevant categories, minus usage-promoted categories.  Always
    keeps always-relevant categories untouched.
    """
    relevant = infer_relevant_categories(cfg_get, available_toolsets, available_tools)
    if not relevant:
        # No toolset info → keep existing config only
        return frozenset(existing_compact or ())

    demote: Set[str] = set()
    for cat in all_cats:
        if cat not in relevant:
            demote.add(cat)

    # Merge with existing explicit config, never undemote user choices
    demote.update(existing_compact or ())
    # Never demote always-relevant categories
    demote -= set(cfg_get("always_relevant"))
    # Usage-promoted categories stay fully visible (dynamic signal)
    demote -= frequent_categories(cfg_get, usage, skill_map, decayed_score)

    return frozenset(demote)
