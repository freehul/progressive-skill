---
name: progressive-skill
description: Compress a bloated skills index to save context tokens. Use when the skill list is too long — decide which categories to demote and budget-cap the rendered index via the bundled CLI.
---

# Progressive Skill (agent-agnostic core)

Shrinks a skill index from full descriptions to compact lines while keeping discovery working. The decision engine (`core/`) is agent-agnostic — drive it with the bundled `cli.py`, feed it your own snapshot/usage data.

## When to use

- The system prompt skill index is too long (100+ skills, thousands of tokens of fixed overhead)
- You want usage-frequency learning: frequently-used skills keep their category fully visible

## Commands

### 1. Decide which categories to demote

```bash
python cli.py demote \
  --snapshot <skills-snapshot.json> \
  --usage usage.json \
  --toolsets terminal,web \
  --tools web_search,read_file
```

Output (JSON):

```json
{
  "demote": ["design", "leadership"],
  "relevant": ["devops", "github", "hermes", "software-development"],
  "all_categories": ["design", "devops", "github", "hermes", "leadership", "software-development"]
}
```

`demote` = categories to collapse into count lines. `relevant` = categories to keep fully visible. If no toolset info is available, demote is empty (safety net).

Snapshot format (the `skills` array, one entry per skill):

```json
{"skills": [{"category": "devops/git", "frontmatter_name": "devops/git"}, ...]}
```

### 2. Budget-compress a rendered index

```bash
python cli.py budget --input index.txt --usage usage.json --relevant devops,hermes
# or pipe:  cat index.txt | python cli.py budget --usage usage.json --relevant devops,hermes
```

Input format — a rendered skills index with these line shapes:

```
  design [names only]: skills/ascii, skills/timeline     ← becomes:   design (2)
  devops
    - devops/git: version control ops                    ← full-category block, budget-capped
```

Output: same text with names-only lines collapsed to `cat (N)` count lines and full-category blocks truncated to the budget (high-usage skills first).

If `--relevant` is omitted, it is derived from `demote` (all categories minus demoted).

## Usage data (optional)

`usage.json` — `{"<skill_name>": {"count": N, "last_used": <unix>}}`. Missing file = cold start (no promotions, everything demoted by toolset mapping only). Decay: `score = count × exp(-Δdays / decay_days)`.

## Config (optional)

`--yaml config.yaml` with a `config:` section, or edit `core/config.py` defaults. Tunables: `list_budget_chars` (4600), `promote_score` (2.0), `decay_days` (30.0), `always_relevant` (["hermes","software-development"]), `phase3_health_check` (true).
