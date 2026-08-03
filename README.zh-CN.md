# Progressive Skill（渐进式技能）

**Hermes Agent 插件**——智能压缩技能索引，按工具集与使用频次渐进式披露，硬预算控制 token 开销。

解决 [NousResearch/hermes-agent#22620](https://github.com/NousResearch/hermes-agent/issues/22620)（*"技能列表膨胀导致上下文窗口暴涨"*）：

安装 250+ 个技能后，Hermes 会把全部技能的名称+描述注入 system prompt——每轮对话固定开销约 **6,000 tokens**。本插件将其压缩至 **约 1,800 tokens（-70%）**，同时保持 Agent 发现并加载正确技能的能力。

## 这是什么

一个 **Hermes 后端插件**（Python），安装到 Hermes 的用户插件目录，通过 `hermes plugins enable` 启用。它不修改 Hermes 源码、不改变渲染逻辑，只做三件事：决定哪些技能分类该降级、记录技能使用频次、按预算截断完整描述。

## 工作原理

三层机制，可独立关闭：

### 1. 工具集决策注入
构建技能索引时，仅依据**强工具集→分类映射**（terminal→devops/github、web→research……）决定哪些分类完整展示，其余降级为单行：

```
leadership (25)        ← 原来是 25 个技能名+描述
books/comfyui-docs (14)
```

降级渲染由 Hermes **原生** `compact_categories` 机制完成——插件只决定*降级什么*，从不重新渲染。若上游改动函数签名，wrapper 捕获 `TypeError` 后自动退化为普通调用（插件透明化，绝不影响 Agent 主循环）。

### 2. 使用频次学习（动态优先级）
技能列表是静态的，但**使用是动态的**。插件 hook `post_tool_call`，记录每次 `skill_view` / `skill_manage` 调用到 `usage.json`：

```
reasonix:      count=3, score=3.00 → autonomous-ai-agents（提升为完整展示）
llm-wiki:      count=1, score=1.00 → 不提升
```

评分 = `count × exp(-Δdays / 30)`（带衰减）。高频使用的技能即使没有工具集映射，其所在分类也保持完整展示——这是静态映射给不了的动态信号。

### 3. Token 硬预算
完整分类按可配置预算截断（`_LIST_BUDGET_CHARS`，默认 4600 字符 ≈ 1,150 tokens）：

- **有使用记录（正分）的技能**优先保留——高分在前
- **零分技能**按原始顺序填充剩余预算
- 被截掉的技能仍可通过 `skills_list(category=...)` 完整发现

## 效果

| 场景 | 优化前 | 优化后 | 节省 |
|---|---|---|---|
| 桌面全工具集 | ~6,100 tok | ~1,800 tok | **-70%** |
| 纯编码 | ~6,100 tok | ~1,800 tok | -70% |
| 无工具集信息（安全网） | ~6,100 tok | ~6,100 tok | 0%（安全） |

已端到端验证：新会话询问"我们蒸馏了哪些书籍"时，Agent 通过压缩索引正确找到 `books/comfyui-docs` 分类，用 `skills_list` 展开、加载正确技能——发现行为与全量索引完全一致。

## 安装

```bash
# 克隆到用户插件目录
git clone https://github.com/freehul/progressive-skill ~/.hermes/plugins/progressive-skill

# 启用（下个会话生效）
hermes plugins enable progressive-skill
```

Windows 路径：`%LOCALAPPDATA%\hermes\plugins\progressive-skill`

**依赖**：Hermes CLI 或桌面应用（任何含 `agent.prompt_builder.build_skills_system_prompt` 与 `compact_categories` 参数的版本）。

## 配置

所有可调参数在 `plugin.yaml` 的 `config:` 段（v2.1+）。默认值：

| 键 | 默认 | 含义 |
|---|---|---|
| `list_budget_chars` | 4600 | 完整分类技能描述硬预算（约 1,150 tokens） |
| `promote_score` | 2.0 | 分类被提升所需的最低衰减使用分 |
| `decay_days` | 30.0 | 使用分衰减半衰期（天） |
| `always_relevant` | ["hermes", "software-development"] | 永不降级的分类 |
| `phase3_health_check` | true | 预算变换匹配不到任何内容时告警（上游格式漂移） |

改动在下一个会话生效。

## v2.1 更新内容

- **UsageTracker 类**——模块级全局变量替换为封装的线程安全存储（无 `global`，可独立测试）
- **usage.json 原子写入**——临时文件 + fsync + `os.replace`；写入中途崩溃也不会损坏统计数据
- **分类数据缓存**——技能快照按 mtime 变化只读取一次，而非每次构建 prompt 读两遍
- **跨平台路径**——使用 `hermes_constants.get_hermes_home()` 替代硬编码 Windows 路径
- **Phase 3 健康检查**——如果 Hermes 改变索引格式，插件记录告警而非静默失效

## 设计原则

- **决策/渲染分离**——插件只决定*哪些分类降级*，Hermes 负责渲染。不对渲染结果做正则，上游改格式也不受影响
- **零 LLM 决策**——全部披露逻辑是纯规则（工具集映射 + 使用分 + 预算），快、确定、token 可预测
- **默认保守**——降级分类保留数量行，从不彻底隐藏；一切可通过 `skills_list` 一步找回
- **安全退化**——签名变化→透明回退；快照缺失→目录扫描；无使用记录→冷启动

## 文件结构

```
progressive-skill/
├── __init__.py     # 插件本体（约 770 行）
├── plugin.yaml     # 插件清单 + 配置段
└── usage.json      # 运行时生成：技能使用统计
```

## 关联

- English: [README.md](README.md)
- Hermes 官方文档（渐进式披露）：[Working with Skills](https://hermes-agent.nousresearch.com/docs/guides/work-with-skills)
- 上游 issue：[#22620 — Skill list bloat causes massive context window inflation](https://github.com/NousResearch/hermes-agent/issues/22620)
