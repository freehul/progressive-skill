#!/usr/bin/env python3
"""Skills-library audit gate — shared engine for pre-push / pre-receive hooks.

Usage:
  python audit_gate.py <skills_dir> [--max-chars N] [--known-names FILE]

Exit codes: 0 = PASS, 1 = FAIL (block), 2 = engine missing (skip).
Deploy layouts supported:
  - repo checkout : scripts/audit_gate.py + ../generic/core/audit.py
  - hooks dir     : audit_gate.py + audit.py side-by-side
Stdlib-only: safe inside server-side hooks.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

# Force UTF-8 stdout/stderr so the Chinese audit messages survive legacy
# console code pages (e.g. cp1252) instead of crashing mid-report.
for _stream in (sys.stdout, sys.stderr):
    _reconf = getattr(_stream, "reconfigure", None)
    if _reconf is not None:
        try:
            _reconf(encoding="utf-8", errors="replace")
        except Exception:
            pass

_HERE = Path(__file__).resolve().parent
_CANDIDATES = (
    _HERE.parent / "generic" / "core" / "audit.py",   # repo checkout
    _HERE / "audit.py",                                # co-deployed in hooks/
)


def _load_engine():
    for cand in _CANDIDATES:
        if cand.is_file():
            spec = importlib.util.spec_from_file_location("ps_audit", cand)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="audit-gate")
    ap.add_argument("skills_dir")
    ap.add_argument("--max-chars", type=int, default=20000)
    ap.add_argument("--known-names", help="optional registry file (txt/json/yml)")
    args = ap.parse_args(argv)

    engine = _load_engine()
    if engine is None:
        print("[audit] engine module (audit.py) not found — cannot run")
        return 2

    known = engine.parse_known_names(args.known_names) \
        if args.known_names else None
    report = engine.audit_skills_dir(Path(args.skills_dir),
                                     max_chars=args.max_chars,
                                     known_names=known)
    s = report["summary"]
    print(f"[audit] {args.skills_dir}: {s['total']} skills, "
          f"{s['fail']} FAIL, {s['warn']} WARN")
    for sk in report["skills"]:
        for i in sk["issues"]:
            mark = "FAIL" if i["severity"] == "fail" else "WARN"
            print(f"  [{mark}] {sk['name']}: {i['message']}")
    reg = report.get("registry")
    if reg:
        for n in reg["missing_on_disk"]:
            print(f"  [WARN] registry drift: '{n}' registered but missing on disk")
    print(f"[audit] {'PASS' if report['ok'] else 'FAIL'}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
