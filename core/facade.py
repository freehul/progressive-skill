"""ProgressiveCore — agent-facing facade over the decision core.

Each agent binds its own data sources (usage store path, skills snapshot
path, skills dir, optional YAML config) and then drives the same
demotion/budget logic.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from . import config
from .budget import apply_transforms, score_skill_lines
from .catalog import load_category_data
from .scorer import UsageTracker
from .selector import compute_demote_set, infer_relevant_categories


class ProgressiveCore:
    """Facade: binds agent-specific inputs, exposes the decision API."""

    def __init__(
        self,
        tracker: Optional[UsageTracker] = None,
        snapshot_path: Optional[Path] = None,
        skills_dir: Optional[Path] = None,
        yaml_path: Optional[Path] = None,
    ) -> None:
        config.init(yaml_path)
        self.tracker = tracker if tracker is not None else UsageTracker()
        self._snapshot_path = Path(snapshot_path) if snapshot_path else None
        self._skills_dir = Path(skills_dir) if skills_dir else None

    # -- data ------------------------------------------------------------

    def category_data(self) -> Tuple[Set[str], Dict[str, str]]:
        """Return (top_level_categories, skill_name→top_category map)."""
        if self._snapshot_path is None:
            return set(), {}
        return load_category_data(self._snapshot_path, self._skills_dir)

    # -- decisions -------------------------------------------------------

    def infer_relevant(
        self,
        toolsets: "Optional[Set[str]]" = None,
        tools: "Optional[Set[str]]" = None,
    ) -> FrozenSet[str]:
        return infer_relevant_categories(config.get, toolsets, tools)

    def compute_demote(
        self,
        available_tools: "Optional[Set[str]]" = None,
        available_toolsets: "Optional[Set[str]]" = None,
        existing_compact: "Optional[FrozenSet[str]]" = None,
    ) -> FrozenSet[str]:
        cats, skill_map = self.category_data()
        return compute_demote_set(
            config.get,
            cats,
            skill_map,
            self.tracker.snapshot(),
            self.tracker.decayed_score,
            available_tools,
            available_toolsets,
            existing_compact,
        )

    def apply_budget(
        self,
        text: str,
        relevant: FrozenSet[str],
    ) -> str:
        return apply_transforms(
            config.get,
            self.tracker.snapshot(),
            self.tracker.decayed_score,
            text,
            relevant,
        )

    def score_lines(
        self,
        block_lines: List[str],
    ) -> List[Tuple[int, float]]:
        return score_skill_lines(
            self.tracker.snapshot(), block_lines, self.tracker.decayed_score
        )
