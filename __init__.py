"""Progressive-disclosure plugin — smart skill index compaction (v2.1).

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

v2.1 changes (2026-08-04, expert-panel review):
  - UsageTracker class replaces module-level globals (testable, no `global`).
  - Atomic usage.json writes (tmp + fsync + os.replace).
  - Category data loaded once per snapshot mtime (cached), killing the
    duplicate I/O in _load_top_level_categories / _load_skill_category_map.
  - Cross-platform HERMES_HOME lookup via hermes_constants.get_hermes_home()
    instead of a hard-coded AppData/Local path.
  - All tunables (decay, promote threshold, budget, always-relevant list)
    are configurable via plugin config (defaults preserved).
  - Phase 3 gains a health-check: if regexes stop matching the rendered
    index, it logs a warning instead of silently no-oping forever.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from math import exp
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from hermes_cli.plugins import PluginAPI

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Plugin config (P3: all tunables configurable, defaults preserved)
# ---------------------------------------------------------------------------
# Loaded lazily via _load_config(); entries are read from the plugin's
# plugin.yaml `config` section if present, else these defaults.

_DEFAULT_CONFIG: Dict[str, Any] = {
    # Decay half-life-ish constant: score = count × exp(-Δdays / 30)
    "decay_days": 30.0,
    # A category is promoted when its best skill's decayed score ≥ this
    "promote_score": 2.0,
    # Token budget for the skills LIST portion (chars; ≈ 1150 tok)
    "list_budget_chars": 4600,
    # Categories never demoted, regardless of toolset
    "always_relevant": ["hermes", "software-development"],
    # Log a warning when Phase 3 regexes match nothing (upstream drift)
    "phase3_health_check": True,
}

_config: Dict[str, Any] = {}
_config_lock = threading.Lock()


def _load_config() -> Dict[str, Any]:
    """Load plugin config once (idempotent, thread-safe)."""
    global _config
    with _config_lock:
        if _config:
            return _config
        cfg = dict(_DEFAULT_CONFIG)
        # Try to read a plugin.yaml sibling for a `config:` section.
        try:
            yaml_path = Path(__file__).with_name("plugin.yaml")
            if yaml_path.exists():
                import yaml

                raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("config"), dict):
                    cfg.update({k: v for k, v in raw["config"].items()
                                if k in _DEFAULT_CONFIG})
        except Exception as exc:
            logger.warning("progressive-skill: config load failed: %s", exc)
        _config = cfg
        return _config


def _cfg(key: str) -> Any:
    return _load_config().get(key, _DEFAULT_CONFIG[key])


# ---------------------------------------------------------------------------
# Usage frequency tracking (Phase 2) — UsageTracker class (P1)
# ---------------------------------------------------------------------------

_PLUGIN_DIR = Path(__file__).parent
_USAGE_FILE = _PLUGIN_DIR / "usage.json"


class UsageTracker:
    """Thread-safe usage frequency store backed by usage.json.

    Encapsulates the previously module-level globals (_usage,
    _usage_dirty, _usage_loaded, _USAGE_LOCK).  Instantiable for tests
    (inject a different storage path); the module uses one shared
    instance at the bottom.
    """

    def __init__(self, storage_path: Optional[Path] = None) -> None:
        self._path = Path(storage_path) if storage_path else _USAGE_FILE
        self._lock = threading.Lock()
        self._usage: Dict[str, Dict[str, Any]] = {}
        self._dirty = False
        self._loaded = False

    # -- persistence -----------------------------------------------------

    def _ensure_loaded(self) -> None:
        """Load usage.json into memory.  Caller MUST hold self._lock.

        (Split from load() so record/snapshot can call it while already
        holding the lock — threading.Lock is not reentrant, and calling
        load() from inside a locked region deadlocks.)
        """
        if self._loaded:
            return
        try:
            if self._path.exists():
                with open(self._path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._usage = data
        except Exception as exc:
            logger.warning("progressive-skill: usage.json load failed: %s", exc)
            self._usage = {}
        self._loaded = True

    def load(self) -> None:
        """Load usage.json into memory (idempotent, thread-safe)."""
        with self._lock:
            self._ensure_loaded()

    def save(self) -> None:
        """Flush in-memory usage to disk (only when dirty).

        Thread-safe: snapshot under the lock, write outside the lock to
        minimise hold time.  Atomic write via temp-file + fsync +
        os.replace so a crash mid-write never leaves a half-written file.
        """
        with self._lock:
            if not self._dirty:
                return
            snapshot = dict(self._usage)
            self._dirty = False

        tmp_path = self._path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
        except Exception as exc:
            logger.warning("progressive-skill: usage.json save failed: %s", exc)
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass

    # -- accessors -------------------------------------------------------

    def record(self, skill_name: str) -> None:
        """Record one skill usage.  Thread-safe, memory-only (flush on save)."""
        if not skill_name:
            return
        with self._lock:
            self._ensure_loaded()
            now = time.time()
            entry = self._usage.get(skill_name)
            if entry and isinstance(entry, dict):
                entry["count"] = int(entry.get("count", 0)) + 1
                entry["last_used"] = now
            else:
                self._usage[skill_name] = {"count": 1, "last_used": now}
            self._dirty = True

    def decayed_score(self, count: int, last_used: float) -> float:
        """Frequency score with recency decay."""
        days = max(0.0, (time.time() - last_used) / 86400.0)
        return float(count) * exp(-days / _cfg("decay_days"))

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Thread-safe shallow copy of current usage data."""
        with self._lock:
            self._ensure_loaded()
            return dict(self._usage)


# Shared module instance (single consumer)
_tracker = UsageTracker()


def _record_usage(skill_name: str) -> None:
    _tracker.record(skill_name)


def _decayed_score(count: int, last_used: float) -> float:
    return _tracker.decayed_score(count, last_used)


def _usage_snapshot() -> Dict[str, Dict[str, Any]]:
    return _tracker.snapshot()


# ---------------------------------------------------------------------------
# Category discovery — single loader + cache (P1/P2 merged)
# ---------------------------------------------------------------------------
# One function loads BOTH top-level categories and the skill→category map
# from the same snapshot read, cached by snapshot mtime.  Cold path falls
# back to scanning the skills directory.  This kills the duplicate I/O
# flagged in review (snapshot read twice per prompt build).

# Cache: {"mtime": float|None, "categories": set, "skill_map": dict}
_cat_cache: Dict[str, Any] = {}
_cat_cache_lock = threading.Lock()


def _snapshot_path() -> Path:
    """Cross-platform snapshot path under HERMES_HOME (P3)."""
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / ".skills_prompt_snapshot.json"
    except Exception:
        # Fallback: env var, then platform defaults — never hard-code.
        home = os.environ.get("HERMES_HOME")
        if home:
            return Path(home) / ".skills_prompt_snapshot.json"
        return Path.home() / ".hermes" / ".skills_prompt_snapshot.json"


def _load_category_data() -> Tuple[set[str], Dict[str, str]]:
    """Return (top_level_categories, skill_name→top_category map).

    Reads the skills snapshot once, caching by file mtime.  Falls back
    to scanning skills directories when the snapshot is missing.
    Returns (set(), {}) only on total failure — callers then demote
    nothing, which is the safe fallback.
    """
    global _cat_cache
    with _cat_cache_lock:
        snap_path = _snapshot_path()
        try:
            mtime = snap_path.stat().st_mtime if snap_path.exists() else None
        except OSError:
            mtime = None

        cached = _cat_cache.get("mtime")
        if cached is not None and cached == mtime and _cat_cache.get("loaded"):
            return _cat_cache["categories"], _cat_cache["skill_map"]

        cats: set[str] = set()
        skill_map: Dict[str, str] = {}

        # Path 1: snapshot (fast, warm path)
        try:
            if snap_path.exists():
                with open(snap_path, encoding="utf-8") as f:
                    snapshot = json.load(f)
                for entry in snapshot.get("skills", []):
                    cat = entry.get("category") or ""
                    if cat:
                        top = cat.split("/", 1)[0]
                        cats.add(top)
                        name = entry.get("frontmatter_name") or entry.get("skill_name")
                        if name:
                            skill_map[name] = top
                if cats:
                    _cat_cache = {
                        "mtime": mtime,
                        "categories": cats,
                        "skill_map": skill_map,
                        "loaded": True,
                    }
                    return cats, skill_map
        except Exception as exc:
            logger.warning("progressive-skill: snapshot read failed: %s", exc)

        # Path 2: scan skills directories (cold path)
        try:
            from hermes_constants import get_skills_dir

            skills_dir = get_skills_dir()
            if skills_dir and skills_dir.exists():
                for p in skills_dir.rglob("SKILL.md"):
                    rel = p.relative_to(skills_dir)
                    if len(rel.parts) >= 2:
                        cats.add(rel.parts[0])
                        skill_map.setdefault(rel.parts[-2], rel.parts[0])
        except Exception as exc:
            logger.warning("progressive-skill: skills dir scan failed: %s", exc)

        if cats:
            _cat_cache = {
                "mtime": mtime,
                "categories": cats,
                "skill_map": skill_map,
                "loaded": True,
            }
        return cats, skill_map


def _load_top_level_categories() -> set[str]:
    cats, _ = _load_category_data()
    return cats


def _load_skill_category_map() -> Dict[str, str]:
    _, skill_map = _load_category_data()
    return skill_map


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


def _infer_relevant_categories(
    toolsets: "set[str] | None" = None,
    tools: "set[str] | None" = None,
) -> frozenset[str]:
    """Return the set of top-level category prefixes relevant to the
    active toolset.  Everything else is a demotion candidate."""
    if not toolsets and not tools:
        return frozenset()  # no info → demote nothing (safety net)

    relevant: set[str] = set(_cfg("always_relevant"))

    for ts in (toolsets or set()):
        relevant.update(_TOOLSET_CATEGORY_MAP.get(ts, ()))

    for t in (tools or set()):
        ts = _TOOL_TO_TOOLSET.get(t)
        if ts:
            relevant.update(_TOOLSET_CATEGORY_MAP.get(ts, ()))

    return frozenset(relevant)


def _frequent_categories() -> frozenset[str]:
    """Return top-level categories promoted by usage frequency.

    A category is promoted when ANY of its skills has a decayed score
    ≥ promote_score (configurable).  Promoted categories are NOT demoted
    even when no toolset mapping points at them.
    """
    usage = _usage_snapshot()
    if not usage:
        return frozenset()
    skill_cat = _load_skill_category_map()

    threshold = float(_cfg("promote_score"))
    promoted: set[str] = set()
    for skill_name, entry in usage.items():
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
        if score >= threshold - 1e-6:
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
    categories.  Always keeps always-relevant categories untouched.
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
    demote -= set(_cfg("always_relevant"))
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
# markers, transforms no-op (safe) — with a health-check warning (P2).

_NAMES_ONLY_RE = re.compile(
    r"^(\s+)(\S+(?:/\S+)*) \[names only\]:\s*(.*)$"
)
# Note: [^\s:]+ excludes the colon so the captured name is clean
# ((\S+) would greedily capture "name:" and miss the usage lookup).
_SKILL_LINE_RE2 = re.compile(r"^(    - )([^\s:]+)(: .*)?$")

# Whether Phase 3 matched anything on the last run (health check, P2)
_last_phase3_matched = False
_phase3_health_lock = threading.Lock()


def _score_skill_lines(block_lines: List[str]) -> List[Tuple[int, float]]:
    """Score each skill line in a full-category block by usage frequency.

    Returns [(line_index, decayed_score)] for lines with a usable name.
    Zero-score lines are kept (score 0 → they sort last but are not
    dropped by the budget logic below unless truly over budget).
    """
    usage = _usage_snapshot()

    scored: List[Tuple[int, float]] = []
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
    global _last_phase3_matched
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
    lines: List[str] = []
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

    out: List[str] = []
    used_chars = 0  # budget consumed by full-category skill lines only
    fixed_budget = int(_cfg("list_budget_chars"))

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
            block_lines: List[str] = []
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

    # Health check (P2): if nothing matched, upstream formatting likely
    # changed — warn once so the failure isn't silent forever.
    with _phase3_health_lock:
        matched_now = len(out) != len(lines) or any(
            "[names only]" not in l and _NAMES_ONLY_RE.match(l) for l in out
        )
        if _cfg("phase3_health_check") and not matched_now and _last_phase3_matched:
            logger.warning(
                "progressive-skill: Phase 3 transforms matched nothing — "
                "Hermes may have changed the skills index format; "
                "budget post-processing is now a no-op"
            )
        _last_phase3_matched = matched_now

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Monkey-patch: wrap build_skills_system_prompt (decision injection)
# ---------------------------------------------------------------------------

_patch_lock = threading.Lock()
_patched = False


def _patch_prompt_builder() -> bool:
    """Install the v2 wrapper.  Idempotent + thread-safe.  True on success."""
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
            logger.info("progressive-skill: patched (v2.1, decision injection) ✓")
        except AttributeError:
            logger.info("progressive-skill: patched (v2.1) ✓ (cache clear N/A)")

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
        _record_usage(name)


def _on_session_end(
    session_id: str = "",
    completed: bool = True,
    interrupted: bool = False,
    **_: Any,
) -> None:
    """Flush usage stats to disk at session end."""
    _tracker.save()
