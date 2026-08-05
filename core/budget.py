"""Budget-driven rendering post-process (agent-agnostic, pattern-locked).

Two conservative transforms on a rendered skills index:
  A. "[names only]: skill1, skill2..." → "  cat (N)"        (count line)
  B. full-category skill lines         → top-N by usage freq (budget cap)
Patterns are locked to stable markers; if the upstream format changes,
transforms no-op (safe) — with a health-check warning.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

NAMES_ONLY_RE = re.compile(r"^(\s+)(\S+(?:/\S+)*) \[names only\]:\s*(.*)$")
# Note: [^\s:]+ excludes the colon so the captured name is clean
# ((\S+) would greedily capture "name:" and miss the usage lookup).
SKILL_LINE_RE2 = re.compile(r"^(    - )([^\s:]+)(: .*)?$")

# Whether Phase 3 matched anything on the last run (health check)
_last_phase3_matched = False
_phase3_health_lock = threading.Lock()


def score_skill_lines(
    usage: Dict[str, Any],
    block_lines: List[str],
    decayed_score: Callable[[int, float], float],
) -> List[Tuple[int, float]]:
    """Score each skill line in a full-category block by usage frequency.

    Returns [(line_index, decayed_score)] for lines with a usable name.
    Zero-score lines are kept (score 0 → they sort last but are not
    dropped by the budget logic below unless truly over budget).
    """
    scored: List[Tuple[int, float]] = []
    for idx, bl in enumerate(block_lines):
        m = SKILL_LINE_RE2.match(bl)
        if not m:
            scored.append((idx, 0.0))
            continue
        skill_name = m.group(2)
        entry = usage.get(skill_name)
        if isinstance(entry, dict):
            try:
                sc = decayed_score(
                    int(entry.get("count", 0)),
                    float(entry.get("last_used", 0) or 0),
                )
            except (TypeError, ValueError):
                sc = 0.0
        else:
            sc = 0.0
        scored.append((idx, sc))
    return scored


def apply_transforms(
    cfg_get: Callable[[str], Any],
    usage: Dict[str, Any],
    decayed_score: Callable[[int, float], float],
    text: str,
    relevant: "frozenset[str]",
) -> str:
    """Apply transforms A + B to the rendered skills index.

    Budget accounting: only FULL-category skill lines consume budget.
    Count lines (A) and headers/footers are free.
    """
    global _last_phase3_matched
    if not relevant:
        return text

    # Pre-pass: merge multi-line descriptions (some skill descriptions
    # contain newlines, which split a category block mid-way and break
    # block collection).  A continuation line is a line that is NOT a
    # category header, NOT a skill line, NOT a names-only line, and NOT
    # blank — append it to the previous line.
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
    fixed_budget = int(cfg_get("list_budget_chars"))

    i = 0
    while i < len(lines):
        line = lines[i]

        # A: names-only → count line (free, no budget)
        m = NAMES_ONLY_RE.match(line)
        if m:
            indent, cat_name, skill_list = m.groups()
            n = len([s for s in skill_list.split(",") if s.strip()])
            out.append(f"{indent}{cat_name} ({n})")
            i += 1
            continue

        # B: full category block → keep top-N skills by usage score
        m = SKILL_LINE_RE2.match(line)
        if m:
            # Gather the whole block first.
            block_lines: List[str] = []
            while i < len(lines) and SKILL_LINE_RE2.match(lines[i]):
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
                scored = score_skill_lines(usage, block_lines, decayed_score)
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

    # Health check: if nothing matched, upstream formatting likely
    # changed — warn once so the failure isn't silent forever.
    with _phase3_health_lock:
        matched_now = len(out) != len(lines) or any(
            "[names only]" not in l and NAMES_ONLY_RE.match(l) for l in out
        )
        if cfg_get("phase3_health_check") and not matched_now and _last_phase3_matched:
            logger.warning(
                "progressive-skill: Phase 3 transforms matched nothing — "
                "the skills index format may have changed; "
                "budget post-processing is now a no-op"
            )
        _last_phase3_matched = matched_now

    return "\n".join(out)
