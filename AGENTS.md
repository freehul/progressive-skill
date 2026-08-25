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
│   │   ├── audit.py       # structural audit rules (monolith/frontmatter/links/layout/registry)
│   │   └── facade.py      # ProgressiveCore — binds data sources, drives decisions
│   ├── cli.py         # universal CLI: demote / budget / audit (no agent deps)
│   └── README.md      # 独立包使用说明
├── scripts/           # 守门工具：audit_gate.py（引擎）+ pre-push / pre-receive 钩子
├── skills/progressive-skill/SKILL.md   # Claude Code skill entry point
└── tests/             # test_core.py + test_audit.py (pytest)
```

## Skills audit gate（技能库结构守门）

```bash
# 手动审计任意技能库（JSON 报告；exit 1 = 有 FAIL）
python generic/cli.py audit <your-skills-dir> --max-chars 20000

# 客户端预推送钩子（可 --no-verify 绕过）
cp scripts/pre-push .git/hooks/pre-push

# 服务端预接收钩子（部署到裸仓 hooks/，--no-verify 无法绕过；
# 与 audit_gate.py + audit.py 同目录放置即可运行）
scp scripts/pre-receive scripts/audit_gate.py scripts/audit.py \
    user@your-server:/path/to/repo.git/hooks/
```

五条规则：R1 单体超预算（未外置=FAIL，已外置=WARN）；R2 frontmatter
合法性与 name↔目录一致；R3 references/assets 引用断链（跳过代码块与
占位符）；R4 目录深度（1–3 层目录合法）；登记一致性（--known-names，
支持 MANIFEST.yaml / json / txt，磁盘缺失或未登记 → WARN）。
退出码约定：0 PASS / 1 FAIL / 2 工具缺失（钩子侧跳过不拦截）。

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
