# Progressive Skill — Generic Core (agent-agnostic)

独立通用包：`core/`（纯 Python 决策核心）+ `cli.py`（通用 CLI）。**零 Hermes 依赖**，任何 agent（Claude Code、Codex、自定义 agent）都能直接驱动同一套 skill 索引压缩决策。

Hermes 插件适配层在本仓库根目录（`__init__.py` + `plugin.yaml`），与本包完全解耦——本包可单独拷贝到任意项目使用。

## 安装

无第三方依赖（仅 Python 标准库）。拷贝 `generic/` 目录到你的项目，或直接引用：

```bash
# 方式一：直接跑 CLI（无需安装）
python generic/cli.py --help

# 方式二：作为 Python 包导入
import sys
sys.path.insert(0, "/path/to/generic")
from core import ProgressiveCore, UsageTracker
```

## CLI 用法

### 1. 决定哪些分类降级压缩（demote）

```bash
python generic/cli.py demote \
  --snapshot <skills-snapshot.json> \
  --usage usage.json \
  --toolsets terminal,web \
  --tools web_search,read_file
```

输出（JSON）：

```json
{
  "demote": ["design", "leadership"],
  "relevant": ["devops", "github", "hermes", "software-development"],
  "all_categories": ["design", "devops", "github", "hermes", "leadership", "software-development"]
}
```

`demote` = 压缩为计数行的分类；`relevant` = 保持完整可见的分类。无 toolset 信息时 demote 为空（安全网）。

Snapshot 格式（skills 数组，每个 skill 一条）：

```json
{"skills": [{"category": "devops/git", "frontmatter_name": "devops/git"}, ...]}
```

### 2. 预算压缩已渲染的索引（budget）

```bash
python generic/cli.py budget --input index.txt --usage usage.json --relevant devops,hermes
# 或管道：cat index.txt | python generic/cli.py budget --usage usage.json --relevant devops,hermes
```

输入格式——渲染后的 skills 索引行：

```
  design [names only]: skills/ascii, skills/timeline     ← 变为：design (2)
  devops
    - devops/git: version control ops                    ← 完整分类块，预算截断
```

输出：names-only 行折叠为 `cat (N)` 计数行，完整分类块按预算截断（高使用优先）。

`--relevant` 省略时由 demote 推导（全部分类减去 demoted）。

## 使用数据（可选）

`usage.json` — `{"<skill_name>": {"count": N, "last_used": <unix>}}`。文件缺失 = 冷启动（无提升，仅按 toolset 映射降级）。衰减：`score = count × exp(-Δdays / decay_days)`。

## 配置（可选）

`--yaml config.yaml`（含 `config:` 段），或改 `core/config.py` 默认值。可调项：`list_budget_chars` (4600)、`promote_score` (2.0)、`decay_days` (30.0)、`always_relevant` (["hermes","software-development"])、`phase3_health_check` (true)。

## 测试

```bash
python -m pytest ../tests/   # 在本仓库根目录运行
```

## 设计

- `core/config.py` — 可调参数（衰减、提升阈值、预算、常相关）
- `core/catalog.py` — 从 snapshot JSON / skills 目录发现分类
- `core/scorer.py` — UsageTracker：线程安全使用统计，近因衰减
- `core/selector.py` — 降级决策（toolset 映射 + 使用提升）
- `core/budget.py` — 预算封顶的索引渲染（计数行 + top-N）
- `core/facade.py` — ProgressiveCore 门面：绑定数据源，驱动决策
