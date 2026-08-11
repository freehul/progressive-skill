#!/usr/bin/env python3
"""progressive-skill CLI — drive the decision core from any agent.

Zero Hermes dependency: feed it your snapshot/usage data, get JSON
decisions or budget-compressed index text.

Examples:
  # Which categories to demote?
  python cli.py demote --snapshot .skills_prompt_snapshot.json \
      --skills-dir ./skills --usage usage.json --toolsets terminal,web

  # Budget-compress a rendered skills index (stdin or --input file)
  python cli.py budget --input index.txt --usage usage.json --relevant hermes,research
  python cli.py budget < index.txt --usage usage.json --snapshot snap.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core import ProgressiveCore, UsageTracker


def _build_core(args: argparse.Namespace) -> ProgressiveCore:
    return ProgressiveCore(
        tracker=UsageTracker(Path(args.usage)) if args.usage else None,
        snapshot_path=Path(args.snapshot) if args.snapshot else None,
        skills_dir=Path(args.skills_dir) if args.skills_dir else None,
        yaml_path=Path(args.yaml) if args.yaml else None,
    )


def _parse_set(value: str) -> set:
    if not value:
        return set()
    return {v.strip() for v in value.split(",") if v.strip()}


def cmd_demote(args: argparse.Namespace) -> int:
    """Decide which top-level categories to demote; emit JSON."""
    core = _build_core(args)
    tools = _parse_set(args.tools)
    toolsets = _parse_set(args.toolsets)
    existing = frozenset(_parse_set(args.existing_compact))
    demote = core.compute_demote(tools, toolsets, existing)
    cats, _ = core.category_data()
    relevant = core.infer_relevant(toolsets, tools)
    out = {
        "demote": sorted(demote),
        "relevant": sorted(relevant),
        "all_categories": sorted(cats),
    }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_budget(args: argparse.Namespace) -> int:
    """Budget-compress a rendered skills index; emit text."""
    core = _build_core(args)
    if args.input:
        text = Path(args.input).read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()

    relevant = frozenset(_parse_set(args.relevant))
    if not relevant:
        # Derive relevant = all categories minus demoted ones.
        cats, _ = core.category_data()
        demote = core.compute_demote(
            _parse_set(args.tools), _parse_set(args.toolsets), None
        )
        relevant = frozenset(cats - set(demote))

    sys.stdout.write(core.apply_budget(text, relevant))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="progressive-skill", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_demote = sub.add_parser("demote", help="decide which categories to demote")
    p_demote.add_argument("--snapshot", help="path to skills snapshot JSON")
    p_demote.add_argument(
        "--skills-dir", help="path to skills directory (cold-path scan)"
    )
    p_demote.add_argument(
        "--usage", help="path to usage.json (default: ./usage.json)"
    )
    p_demote.add_argument(
        "--yaml", help="optional config YAML carrying a `config:` section"
    )
    p_demote.add_argument("--toolsets", help="comma-separated active toolsets")
    p_demote.add_argument("--tools", help="comma-separated active tools")
    p_demote.add_argument(
        "--existing-compact", help="comma-separated categories already compacted"
    )
    p_demote.set_defaults(func=cmd_demote)

    p_budget = sub.add_parser(
        "budget", help="budget-compress a rendered skills index"
    )
    p_budget.add_argument(
        "--input", help="file with rendered index (default: read stdin)"
    )
    p_budget.add_argument(
        "--usage", help="path to usage.json (default: ./usage.json)"
    )
    p_budget.add_argument(
        "--yaml", help="optional config YAML carrying a `config:` section"
    )
    p_budget.add_argument(
        "--relevant", help="comma-separated relevant categories "
        "(if omitted, derived from demote)"
    )
    p_budget.add_argument(
        "--snapshot", help="snapshot JSON (used when deriving relevant)"
    )
    p_budget.add_argument(
        "--skills-dir", help="skills dir (used when deriving relevant)"
    )
    p_budget.add_argument(
        "--toolsets", help="comma-separated toolsets (used when deriving relevant)"
    )
    p_budget.add_argument(
        "--tools", help="comma-separated tools (used when deriving relevant)"
    )
    p_budget.set_defaults(func=cmd_budget)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
