# Progressive Skill

**Smart skill index compaction** with progressive disclosure, budget control, and usage-frequency learning — **works with any agent**.

Tackles [NousResearch/hermes-agent#22620](https://github.com/NousResearch/hermes-agent/issues/22620): *"Skill list bloat causes massive context window inflation."*

With 250+ skills installed, the full index (name + description for every skill) is injected into the system prompt on every turn — ~6,000 tokens of fixed overhead. Progressive Skill shrinks that to **~1,800 tokens (-70%)** while keeping the agent's ability to discover and load the right skill.

## What this is

Since **v3**, the project splits into an **agent-agnostic decision core** (`core/`, pure Python, zero agent imports) and per-agent adapters:

- **Hermes**: a thin backend plugin (`__init__.py`) installed via `hermes plugins install freehul/progressive-skill --enable`. It decides which skill categories to demote, records usage frequency, and truncates full descriptions to a budget — without modifying Hermes source.
- **Any other agent (Claude Code, Codex, ...)**: drive the same decisions through the bundled `cli.py` — see [Universal agent usage](#universal-agent-usage) below.

## How it works

Three layers, each independently disabled:

### 1. Toolset decision injection
When building the skills index, only **strong toolset→category links** (terminal→devops/github, web→research, …) decide which categories stay fully visible. Everything else is demoted to a single compact line:

```
leadership (25)        ← was: 25 skill names + descriptions
books/comfyui-docs (14)
```

Demotion is handled by Hermes's **native** `compact_categories` mechanism — the plugin only decides *what* to demote, never re-renders. If upstream changes the function signature, the wrapper catches `TypeError` and degrades to a plain call (the plugin becomes transparent, never breaks the agent loop).

### 2. Usage-frequency learning (dynamic priority)
Skill lists are static, but **usage is dynamic**. The plugin hooks `post_tool_call` to record every `skill_view` / `skill_manage` call into `usage.json`:

```
reasonix:      count=3, score=3.00 → autonomous-ai-agents (promoted)
llm-wiki:      count=1, score=1.00 → not promoted
```

Score = `count × exp(-Δdays / 30)` — recency-decayed, so frequently-used skills keep their whole category fully visible even without a toolset mapping. This is the dynamic signal a static mapping can't provide.

### 3. Token budget (hard cap)
Full categories are truncated to a configurable budget (`_LIST_BUDGET_CHARS`, default 4600 chars ≈ 1,150 tokens):

- **Positive-scored skills** (used recently) are kept first — highest first
- **Zero-scored skills** fill the remaining budget in original order
- Dropped skills remain fully discoverable via `skills_list(category=...)`

## Results

| Scenario | Before | After | Savings |
|---|---|---|---|
| Desktop (full toolset) | ~6,100 tok | ~1,800 tok | **-70%** |
| Pure coding | ~6,100 tok | ~1,800 tok | -70% |
| No toolset info (safety) | ~6,100 tok | ~6,100 tok | 0% (safe) |

Verified end-to-end: a fresh session asking "what books have we distilled" correctly found the `books/comfyui-docs` category through the compact index, expanded it with `skills_list`, and loaded the right skills — identical discovery behavior to the full index.

## Installation

### Hermes (official plugin flow)

```bash
hermes plugins install freehul/progressive-skill --enable
# updates: hermes plugins update progressive-skill
```

Requires Hermes CLI or desktop app (any version with `agent.prompt_builder.build_skills_system_prompt` and the `compact_categories` kwarg).

### Universal agent usage

Any agent can run the decision core directly — no Hermes required. Full guide in [`skills/progressive-skill/SKILL.md`](skills/progressive-skill/SKILL.md) and `AGENTS.md`.

```bash
# Which categories to demote? (JSON out)
python cli.py demote --snapshot snap.json --usage usage.json --toolsets terminal,web

# Budget-compress a rendered skills index
python cli.py budget --input index.txt --usage usage.json --relevant devops,hermes
```

Prerequisites: Python 3.10+, your own skills snapshot JSON (one entry per skill: `{"category": "...", "frontmatter_name": "..."}`) and optional `usage.json`.

## Configuration

All tunables live in `plugin.yaml` under the `config:` section (v2.1+). Defaults:

| Key | Default | Meaning |
|---|---|---|
| `list_budget_chars` | 4600 | Hard budget for full-category skill descriptions (~1,150 tok) |
| `promote_score` | 2.0 | Decayed usage score needed to promote a category |
| `decay_days` | 30.0 | Recency decay half-life for usage scores |
| `always_relevant` | ["hermes", "software-development"] | Categories never demoted |
| `phase3_health_check` | true | Warn when budget transforms match nothing (upstream format drift) |

Changes take effect next session.

## What's new in v2.1

- **UsageTracker class** — module globals replaced by an encapsulated, thread-safe store (no `global`, testable in isolation)
- **Atomic usage.json writes** — temp file + fsync + `os.replace`; a crash mid-write can never corrupt the stats file
- **Category data cache** — the skills snapshot is read once per mtime change instead of twice per prompt build
- **Cross-platform paths** — uses `hermes_constants.get_hermes_home()` instead of a hard-coded Windows path
- **Phase 3 health check** — if Hermes changes the index format, the plugin logs a warning instead of silently no-oping

## What's new in v3

- **Agent-agnostic core** — decision logic extracted to `core/` (pure Python, zero Hermes imports). Hermes plugin is now a thin adapter; other agents drive the same logic via `cli.py`.
- **CLI** — `cli.py demote` / `cli.py budget` for universal agent usage
- **Claude Code skill** — `skills/progressive-skill/SKILL.md` entry point
- **Unit tests** — `tests/test_core.py` (11 cases, pytest)

## Design principles

- **Decision/render separation** — the plugin decides *which* categories to demote; Hermes renders. No regex over rendered output → robust to upstream formatting changes.
- **Zero LLM decisions** — all disclosure logic is pure rules (toolset mapping + usage scores + budget). Fast, deterministic, token-predictable.
- **Conservative by default** — demoted categories stay visible as count lines; nothing is ever fully hidden. Everything is one `skills_list` call away.
- **Safe degradation** — signature drift → transparent fallback; missing snapshot → directory scan; missing usage → cold start.

## Files

```
progressive-skill/
├── __init__.py     # Hermes adapter (thin, v3)
├── plugin.yaml     # Hermes plugin manifest + config section
├── cli.py          # universal CLI: demote / budget (no agent deps)
├── core/           # agent-agnostic decision core (config/catalog/scorer/selector/budget/facade)
├── skills/progressive-skill/SKILL.md   # Claude Code skill entry point
├── AGENTS.md       # agent integration guide
├── tests/          # unit tests (pytest)
└── usage.json      # created at runtime: skill usage stats
```

## Related

- 中文说明: [README.zh-CN.md](README.zh-CN.md)
- Official docs on skill progressive disclosure: [Working with Skills](https://hermes-agent.nousresearch.com/docs/guides/work-with-skills)
- Upstream issue: [#22620 — Skill list bloat causes massive context window inflation](https://github.com/NousResearch/hermes-agent/issues/22620)
