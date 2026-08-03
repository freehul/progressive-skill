"""Progressive-disclosure plugin — smart skill index compaction (v2).

v2 architecture: decision/render separation.
  - This plugin ONLY decides WHICH categories get demoted (compact_categories).
  - Hermes's native rendering handles the demotion ([names only] lines).
  - No text post-processing, no regex over rendered output → robust to
    upstream formatting changes.

Handles upstream signature drift: if ``build_skills_system_prompt`` drops
the ``compact_categories`` kwarg, the wrapper catches TypeError and falls
back to a plain call (plugin becomes transparent, never breaks the loop).

Phase 2 (usage frequency): skill_view / skill_manage calls are recorded
into usage.json.  Categories containing frequently-used skills are
promoted (NOT demoted) even when no toolset mapping points at them —
usage frequency is the dynamic signal that static mapping can't provide.

Design decisions (2026-08-04):
  - Budget: 1500 tok target (not enforced yet; Phase 3)
  - Conservative: demoted categories stay visible as [names only]
    (never fully hidden)
  - Narrow mapping: only STRONG toolset→category links.
  - Usage frequency: decay score = count × exp(-Δdays / 30)
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from math import exp
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from hermes_cli.plugins import PluginAPI

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Usage frequency tracking (Phase 2)
# ---------------------------------------------------------------------------

_PLUGIN_DIR = Path(__file__).parent
_USAGE_FILE = _PLUGIN_DIR / "usage.json"
_USAGE_LOCK = threading.Lock()

# Decay half-life-ish constant: score = count × exp(-Δdays / 30)
_DECAY_DAYS = 30.0
# A category is promoted when its best skill's decayed score ≥ this
_PROMOTE_SCORE = 2.0

# In-memory usage: {skill_name: {"count": int, "last_used": float}}
_usage: Dict[str, Dict[str, Any]] = {}
_usage_dirty = False
_usage_loaded = False


def _load_usage() -> None:
    """Load usage.json into memory (idempotent)."""
    global _usage, _usage_loaded
    if _usage_loaded:
        return
    try:
        if _USAGE_FILE.exists():
            with open(_USAGE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _usage = data
    except Exception as exc:
        logger.warning("progressive-skill: usage.json load failed: %s", exc)
        _usage = {}
    _usage_loaded = True


def _save_usage() -> None:
    """Flush in-memory usage to disk (only when dirty)."""
    global _usage_dirty
    if not _usage_dirty:
        return
    try:
        with open(_USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(_usage, f, ensure_ascii=False, indent=1)
        _usage_dirty = False
    except Exception as exc:
        logger.warning("progressive-skill: usage.json save failed: %s", exc)


def _record_usage(skill_name: str) -> None:
    """Record one skill usage.  Thread-safe, memory-only (flush on save)."""
    if not skill_name:
        return
    global _usage_dirty
    with _USAGE_LOCK:
        _load_usage()
        now = time.time()
        entry = _usage.get(skill_name)
        if entry and isinstance(entry, dict):
            entry["count"] = int(entry.get("count", 0)) + 1
            entry["last_used"] = now
        else:
            _usage[skill_name] = {"count": 1, "last_used": now}
        _usage_dirty = True


def _decayed_score(count: int, last_used: float) -> float:
    """Frequency score with recency decay."""
    days = max(0.0, (time.time() - last_used) / 86400.0)
    return float(count) * exp(-days / _DECAY_DAYS)


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
        _record_usage(name)


def _on_session_end(
    session_id: str = "",
    completed: bool = True,
    interrupted: bool = False,
    **_: Any,
) -> None:
    """Flush usage stats to disk at session end."""
    _save_usage()


# ---------------------------------------------------------------------------
# Narrowed toolset → relevant category mapping (v2)
# ---------------------------------------------------------------------------
# Only STRONG links survive. Weak/ambiguous links are intentionally absent;
# their categories are demoted by default and can be promoted later by the
# usage-frequency scorer (Phase 2).

_TOOLSET_CATEGORY_MAP: dict[str, tuple[str, ...]] = {
    "terminal": (
        "software-development", "devops", "github", "hermes",
    ),
    "file": (
        "software-development", "hermes",
    ),
    "web": (
        "research", "hermes",
    ),
    "browser": (
        "research", "computer-use",
    ),
    "skills": (
        "hermes",
    ),
    "memory": (
        "note-taking",
    ),
}

_TOOL_TO_TOOLSET: dict[str, str] = {
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

# Categories never demoted, regardless of toolset.
_ALWAYS_RELEVANT: frozenset[str] = frozenset({
    "hermes",
    "software-development",
})


def _infer_relevant_categories(
    toolsets: "set[str] | None" = None,
    tools: "set[str] | None" = None,
) -> frozenset[str]:
    """Return the set of top-level category prefixes relevant to the
    active toolset.  Everything else is a demotion candidate."""
    if not toolsets and not tools:
        return frozenset()  # no info → demote nothing (safety net)

    relevant: set[str] = set(_ALWAYS_RELEVANT)

    for ts in (toolsets or set()):
        relevant.update(_TOOLSET_CATEGORY_MAP.get(ts, ()))

    for t in (tools or set()):
        ts = _TOOL_TO_TOOLSET.get(t)
        if ts:
            relevant.update(_TOOLSET_CATEGORY_MAP.get(ts, ()))

    return frozenset(relevant)


# ---------------------------------------------------------------------------
# Category discovery (top-level names) from the skills snapshot
# ---------------------------------------------------------------------------

def _load_top_level_categories() -> set[str]:
    """Read all top-level category names from the skills snapshot.

    Falls back to scanning the skills directory when the snapshot is
    missing (e.g. cold path right after a cache clear).  Returns empty
    set only on total failure — the wrapper then demotes nothing, which
    is the safe fallback.
    """
    # Path 1: snapshot (fast, warm path)
    try:
        snapshot_path = (
            Path.home() / "AppData/Local/hermes/.skills_prompt_snapshot.json"
        )
        if snapshot_path.exists():
            with open(snapshot_path, encoding="utf-8") as f:
                snapshot = json.load(f)
            cats: set[str] = set()
            for entry in snapshot.get("skills", []):
                cat = entry.get("category") or ""
                if cat:
                    cats.add(cat.split("/", 1)[0])
            if cats:
                return cats
    except Exception as exc:
        logger.warning(
            "progressive-skill: snapshot read failed: %s", exc
        )

    # Path 2: scan skills directory tree (cold path)
    try:
        import agent.prompt_builder as pb_mod
        skills_dir = pb_mod.get_skills_dir()
        cats: set[str] = set()
        if skills_dir and skills_dir.exists():
            for p in skills_dir.rglob("SKILL.md"):
                rel = p.relative_to(skills_dir)
                if len(rel.parts) >= 2:
                    cats.add(rel.parts[0])
        if cats:
            return cats
    except Exception as exc:
        logger.warning(
            "progressive-skill: skills dir scan failed: %s", exc
        )

    return set()


def _load_skill_category_map() -> Dict[str, str]:
    """Build skill_name → top-level category mapping.

    Path 1: skills snapshot (fast).  Path 2: scan skills directory
    (cold path, snapshot missing).  Empty dict on total failure.
    """
    skill_cat: Dict[str, str] = {}
    try:
        snapshot_path = (
            Path.home() / "AppData/Local/hermes/.skills_prompt_snapshot.json"
        )
        if snapshot_path.exists():
            with open(snapshot_path, encoding="utf-8") as f:
                snapshot = json.load(f)
            for entry in snapshot.get("skills", []):
                name = entry.get("frontmatter_name") or entry.get("skill_name")
                cat = entry.get("category") or ""
                if name and cat:
                    skill_cat[name] = cat.split("/", 1)[0]
            if skill_cat:
                return skill_cat
    except Exception as exc:
        logger.warning(
            "progressive-skill: snapshot read failed (map): %s", exc
        )

    try:
        import agent.prompt_builder as pb_mod
        parse_frontmatter = pb_mod.parse_frontmatter

        for sdir in pb_mod.get_all_skills_dirs():
            if not sdir.exists():
                continue
            for skill_file in sdir.rglob("SKILL.md"):
                try:
                    fm, _ = parse_frontmatter(
                        skill_file.read_text(encoding="utf-8")
                    )
                    name = fm.get("name") or skill_file.stem
                    rel = skill_file.relative_to(sdir)
                    cat = rel.parts[0] if len(rel.parts) >= 2 else "general"
                    skill_cat[name] = cat
                except Exception:
                    continue
        return skill_cat
    except Exception as exc:
        logger.warning(
            "progressive-skill: skills dir scan failed (map): %s", exc
        )
        return {}


def _frequent_categories() -> frozenset[str]:
    """Return top-level categories promoted by usage frequency.

    A category is promoted when ANY of its skills has a decayed score
    ≥ _PROMOTE_SCORE (e.g. used 2× within ~30 days, or more times over
    a longer window).  Promoted categories are NOT demoted even when no
    toolset mapping points at them.
    """
    with _USAGE_LOCK:
        _load_usage()
        if not _usage:
            return frozenset()
        skill_cat = _load_skill_category_map()

        promoted: set[str] = set()
        for skill_name, entry in _usage.items():
            if not isinstance(entry, dict):
                continue
            try:
                score = _decayed_score(
                    int(entry.get("count", 0)),
                    float(entry.get("last_used", 0) or 0),
                )
            except (TypeError, ValueError):
                continue
            # Epsilon tolerance: exp() float noise (e.g. 1.999999998)
            # must not block an exact-threshold promotion.
            if score >= _PROMOTE_SCORE - 1e-6:
                cat = skill_cat.get(skill_name)
                if cat:
                    promoted.add(cat)
        return frozenset(promoted)


def _compute_demote_set(
    available_tools: "set[str] | None" = None,
    available_toolsets: "set[str] | None" = None,
    existing_compact: "frozenset[str] | None" = None,
) -> "frozenset[str]":
    """Decide which top-level categories to demote.

    Merge: user-configured compact_categories (from Hermes posture) +
    toolset-inferred non-relevant categories, minus usage-promoted
    categories.  Always keeps _ALWAYS_RELEVANT categories untouched.
    """
    relevant = _infer_relevant_categories(available_toolsets, available_tools)
    if not relevant:
        # No toolset info → keep existing config only
        return frozenset(existing_compact or ())

    all_cats = _load_top_level_categories()
    demote: set[str] = set()

    for cat in all_cats:
        if cat not in relevant:
            demote.add(cat)

    # Merge with existing explicit config, never undemote user choices
    demote.update(existing_compact or ())
    # Never demote always-relevant categories
    demote -= _ALWAYS_RELEVANT
    # Usage-promoted categories stay fully visible (dynamic signal)
    demote -= _frequent_categories()

    return frozenset(demote)


# ---------------------------------------------------------------------------
# Phase 3: budget-driven rendering (post-process, minimal & pattern-locked)
# ---------------------------------------------------------------------------
# Two transforms on the rendered index, both conservative:
#   A. "[names only]: skill1, skill2..."  → "  cat (N)"        (count line)
#   B. full-category skill lines          → top-N by usage freq (budget cap)
# Patterns are locked to Hermes's stable markers; if upstream changes the
# markers, transforms simply no-op (safe).

_NAMES_ONLY_RE = re.compile(
    r"^(\s+)(\S+(?:/\S+)*) \[names only\]:\s*(.*)$"
)
# Note: [^\s:]+ excludes the colon so the captured name is clean
# ((\S+) would greedily capture "name:" and miss the usage lookup).
_SKILL_LINE_RE2 = re.compile(r"^(    - )([^\s:]+)(: .*)?$")

# Token budget for the skills LIST portion (excludes the ~350 tok fixed
# instruction preamble).  Configurable via plugin config later.
_LIST_BUDGET_CHARS = 4600  # ≈ 1150 tok


def _score_skill_lines(block_lines: list[str]) -> list[tuple[int, float]]:
    """Score each skill line in a full-category block by usage frequency.

    Returns [(line_index, decayed_score)] for lines with a usable name.
    Zero-score lines are kept (score 0 → they sort last but are not
    dropped by the budget logic below unless truly over budget).
    """
    with _USAGE_LOCK:
        _load_usage()
        usage = dict(_usage)

    scored: list[tuple[int, float]] = []
    for idx, bl in enumerate(block_lines):
        m = _SKILL_LINE_RE2.match(bl)
        if not m:
            scored.append((idx, 0.0))
            continue
        skill_name = m.group(2)
        entry = usage.get(skill_name)
        if isinstance(entry, dict):
            try:
                sc = _decayed_score(
                    int(entry.get("count", 0)),
                    float(entry.get("last_used", 0) or 0),
                )
            except (TypeError, ValueError):
                sc = 0.0
        else:
            sc = 0.0
        scored.append((idx, sc))
    return scored


def _apply_budget_transforms(
    text: str, relevant: frozenset[str]
) -> str:
    """Apply transforms A + B to the rendered skills index.

    Budget accounting: only FULL-category skill lines consume budget.
    Count lines (A) and headers/footers are free — the budget is
    specifically about keeping detailed skill descriptions bounded.
    """
    if not relevant:
        return text

    # Pre-pass: merge multi-line descriptions (some skill descriptions
    # contain newlines, which split a category block mid-way and break
    # block collection).  A continuation line is a line that is NOT a
    # category header ("  name:"), NOT a skill line ("    - name:"),
    # NOT a names-only line, and NOT blank — append it to the previous
    # line.  Continuation lines can have any indentation (the snapshot
    # truncates long descriptions with a literal newline + ellipsis).
    raw_lines = text.split("\n")
    lines: list[str] = []
    for l in raw_lines:
        is_cat_header = bool(re.match(r"^  \S+(?:/\S+)*\s*:\s*$", l))
        is_skill_line = l.startswith("    - ")
        is_names_only = "[names only]" in l
        is_blank = not l.strip()
        if (
            lines
            and not is_cat_header
            and not is_skill_line
            and not is_names_only
            and not is_blank
        ):
            lines[-1] = lines[-1] + " " + l.strip()
        else:
            lines.append(l)

    out: list[str] = []
    used_chars = 0  # budget consumed by full-category skill lines only
    fixed_budget = _LIST_BUDGET_CHARS

    i = 0
    while i < len(lines):
        line = lines[i]

        # A: names-only → count line (free, no budget)
        m = _NAMES_ONLY_RE.match(line)
        if m:
            indent, cat_name, skill_list = m.groups()
            n = len([s for s in skill_list.split(",") if s.strip()])
            out.append(f"{indent}{cat_name} ({n})")
            i += 1
            continue

        # B: full category block → keep top-N skills by usage score
        m = _SKILL_LINE_RE2.match(line)
        if m:
            # Gather the whole block first.
            block_lines: list[str] = []
            while i < len(lines) and _SKILL_LINE_RE2.match(lines[i]):
                block_lines.append(lines[i])
                i += 1
            block_len = sum(len(l) + 1 for l in block_lines)

            if used_chars + block_len <= fixed_budget:
                # Whole block fits — keep as-is
                out.extend(block_lines)
                used_chars += block_len
            else:
                # Budget cap: positive-scored skills are kept first
                # (usage priority); zero-scored skills fill the remaining
                # budget in original order.
                scored = _score_skill_lines(block_lines)
                scored.sort(key=lambda x: x[1], reverse=True)
                budget_left = max(0, fixed_budget - used_chars)
                kept_set: set[int] = set()

                # Pass 1: positive-scored skills (highest first)
                for idx, sc in scored:
                    if sc <= 0:
                        break  # rest are zero — handled in pass 2
                    cost = len(block_lines[idx]) + 1
                    if budget_left - cost < 0:
                        continue  # too expensive; skip (rare)
                    kept_set.add(idx)
                    budget_left -= cost

                # Pass 2: zero-scored skills in original order, fill
                # remaining budget (keeps discoverability at cold start).
                for idx in range(len(block_lines)):
                    if idx in kept_set:
                        continue
                    cost = len(block_lines[idx]) + 1
                    if budget_left - cost < 0:
                        break
                    kept_set.add(idx)
                    budget_left -= cost

                kept = [block_lines[j] for j in range(len(block_lines))
                        if j in kept_set]
                if kept:
                    out.extend(kept)
                    used_chars += sum(len(k) + 1 for k in kept)
                # (dropped skills are discoverable via skills_list)
            continue

        out.append(line)
        i += 1

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Monkey-patch: wrap build_skills_system_prompt (decision injection)
# ---------------------------------------------------------------------------

_patched = False


def _patch_prompt_builder() -> bool:
    """Install the v2 wrapper.  Idempotent.  Returns True on success."""
    global _patched
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
    ):
        demote = _compute_demote_set(
            available_tools, available_toolsets, compact_categories
        )
        # Decision injection: pass demotion set to native rendering.
        # If upstream dropped the kwarg, fall back to a plain call —
        # the plugin becomes transparent instead of breaking.
        try:
            result = original(
                available_tools=available_tools,
                available_toolsets=available_toolsets,
                compact_categories=demote,
            )
        except TypeError:
            logger.info(
                "progressive-skill: compact_categories kwarg "
                "unsupported upstream; falling back to plain call"
            )
            result = original(
                available_tools=available_tools,
                available_toolsets=available_toolsets,
            )
        if not result:
            return result

        # Phase 3: budget-driven post-process (count lines + freq cap).
        relevant = _infer_relevant_categories(
            available_toolsets, available_tools
        )
        if relevant:
            return _apply_budget_transforms(result, relevant)
        return result

    pb.build_skills_system_prompt = wrapped_build_skills_system_prompt
    _patched = True

    # Clear in-process LRU cache so the next call rebuilds with the patch
    try:
        pb.clear_skills_system_prompt_cache()
        logger.info("progressive-skill: patched (v2, decision injection) ✓")
    except AttributeError:
        logger.info("progressive-skill: patched (v2) ✓ (cache clear N/A)")

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
