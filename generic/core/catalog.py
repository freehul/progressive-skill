"""Category discovery — load from a skills snapshot JSON, fall back to scanning a skills dir.

Pure I/O with caching by snapshot mtime.  The snapshot path and skills dir
are injected by the caller (agent-specific); this module never imports
agent internals.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()


def load_category_data(
    snapshot_path: Path,
    skills_dir: Optional[Path] = None,
) -> Tuple[Set[str], Dict[str, str]]:
    """Return (top_level_categories, skill_name → top_category map).

    Reads the skills snapshot once, caching by file mtime.  Falls back to
    scanning skills directories when the snapshot is missing.  Returns
    (set(), {}) only on total failure — callers then demote nothing,
    which is the safe fallback.
    """
    global _cache
    with _cache_lock:
        try:
            mtime = snapshot_path.stat().st_mtime if snapshot_path.exists() else None
        except OSError:
            mtime = None

        cached = _cache.get("mtime")
        if cached is not None and cached == mtime and _cache.get("loaded"):
            return _cache["categories"], _cache["skill_map"]

        cats: Set[str] = set()
        skill_map: Dict[str, str] = {}

        # Path 1: snapshot (fast, warm path)
        try:
            if snapshot_path.exists():
                with open(snapshot_path, encoding="utf-8") as f:
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
                    _cache = {
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
            if skills_dir is not None and skills_dir.exists():
                for p in skills_dir.rglob("SKILL.md"):
                    rel = p.relative_to(skills_dir)
                    if len(rel.parts) >= 2:
                        cats.add(rel.parts[0])
                        skill_map.setdefault(rel.parts[-2], rel.parts[0])
        except Exception as exc:
            logger.warning("progressive-skill: skills dir scan failed: %s", exc)

        if cats:
            _cache = {
                "mtime": mtime,
                "categories": cats,
                "skill_map": skill_map,
                "loaded": True,
            }
        return cats, skill_map
