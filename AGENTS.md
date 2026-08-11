# AGENTS.md — Progressive Skill

Agent-agnostic skill-index compaction. The decision core (`core/`) is pure
Python with zero agent imports; each agent binds its own data sources.

## Repository layout

```
progressive-skill/
├── __init__.py        # Hermes adapter: paths, monkey-patch, hooks (thin)
├── plugin.yaml        # Hermes plugin manifest + config section
├── generic/           # agent-agnostic 独立包（零 Hermes 依赖，可单独拷贝）
│   ├── core/          # decision core
│   │   ├── config.py      # tunables (decay, promote_score, budget, always_relevant)
│   │   ├── catalog.py     # category discovery from snapshot JSON / skills dir
│   │   ├── scorer.py      # UsageTracker — thread-safe usage store, recency decay
│   │   ├── selector.py    # demote decision (toolset mapping + usage promotion)
│   │   ├── budget.py      # budget-capped index rendering (count lines + top-N)
│   │   └── facade.py      # ProgressiveCore — binds data sources, drives decisions
│   ├── cli.py         # universal CLI: demote / budget (no agent deps)
│   └── README.md      # 独立包使用说明
├── skills/progressive-skill/SKILL.md   # Claude Code skill entry point
└── tests/test_core.py # 11 unit tests (pytest)
```

## For any agent (Claude Code, Codex, ...)

Drive the core through `generic/cli.py` — see `skills/progressive-skill/SKILL.md`
for the full usage guide. Short form:

```bash
python generic/cli.py demote --snapshot snap.json --usage usage.json --toolsets terminal
python generic/cli.py budget --input index.txt --usage usage.json --relevant devops,hermes
```

## For Hermes

The plugin is the thin adapter in `__init__.py`: it binds `usage.json`
(next to the plugin), the skills snapshot under HERMES_HOME, the skills
directory, and `plugin.yaml`, then delegates all decisions to
`ProgressiveCore`. Install via the official flow:

```bash
hermes plugins install freehul/progressive-skill --enable
```

## Development

```bash
python -m venv .venv
.venv/bin/pip install pytest        # Windows: .venv\Scripts\pip
.venv/bin/python -m pytest tests/   # Windows: .venv\Scripts\python -m pytest tests/
```

Behavior is verified equivalent across the refactor by `tests/test_core.py`
(decay scoring, demote decision, usage promotion, budget transforms).
