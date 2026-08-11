"""Tunables — all configurable via an optional YAML `config:` section, defaults preserved."""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_CONFIG: Dict[str, Any] = {
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

_state: Dict[str, Any] = {}
_lock = threading.Lock()


def init(yaml_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load config once (idempotent, thread-safe).

    An optional YAML file (e.g. the Hermes plugin.yaml) may carry a
    `config:` section; only keys present in DEFAULT_CONFIG are applied.
    """
    global _state
    with _lock:
        if _state:
            return _state
        cfg = dict(DEFAULT_CONFIG)
        if yaml_path is not None:
            try:
                if yaml_path.exists():
                    import yaml

                    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict) and isinstance(raw.get("config"), dict):
                        cfg.update(
                            {k: v for k, v in raw["config"].items() if k in DEFAULT_CONFIG}
                        )
            except Exception as exc:
                logger.warning("progressive-skill: config load failed: %s", exc)
        _state = cfg
        return _state


def get(key: str) -> Any:
    if not _state:
        init()
    return _state.get(key, DEFAULT_CONFIG[key])
