"""Usage-frequency scoring — thread-safe store with recency decay (agent-agnostic).

Storage path is injected by the caller; nothing here touches agent APIs.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from math import exp
from pathlib import Path
from typing import Any, Dict, Optional

from . import config

logger = logging.getLogger(__name__)


class UsageTracker:
    """Thread-safe usage frequency store backed by a JSON file.

    Encapsulates the previously module-level globals.  Instantiable for
    tests (inject a different storage path).
    """

    def __init__(self, storage_path: Optional[Path] = None) -> None:
        self._path = Path(storage_path) if storage_path else Path("usage.json")
        self._lock = threading.Lock()
        self._usage: Dict[str, Dict[str, Any]] = {}
        self._dirty = False
        self._loaded = False

    # -- persistence -----------------------------------------------------

    def _ensure_loaded(self) -> None:
        """Load usage data into memory.  Caller MUST hold self._lock."""
        if self._loaded:
            return
        try:
            if self._path.exists():
                with open(self._path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._usage = data
        except Exception as exc:
            logger.warning("progressive-skill: usage load failed: %s", exc)
            self._usage = {}
        self._loaded = True

    def load(self) -> None:
        with self._lock:
            self._ensure_loaded()

    def save(self) -> None:
        """Flush in-memory usage to disk (only when dirty), atomically."""
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
            logger.warning("progressive-skill: usage save failed: %s", exc)
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
        return float(count) * exp(-days / float(config.get("decay_days")))

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Thread-safe shallow copy of current usage data."""
        with self._lock:
            self._ensure_loaded()
            return dict(self._usage)
