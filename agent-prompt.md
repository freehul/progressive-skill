# Agent 提示词（可写入 SOUL.md / USER.md / MEMORY.md）

> 本文件是给 **Agent 安装者**看的：把下面三段分别复制到你自己的记忆文件（SOUL.md / USER.md / MEMORY.md），Agent 就会知道如何高效使用 progressive-skill。三段均可直接整段复制。

---

## 1. SOUL.md 段（行为准则，复制进 SOUL.md）

```markdown
### Progressive Skill（skill 索引压缩）
- 系统提示词中 skill 索引过长（100+ 个 skill、数千 token 固定开销）时，使用 progressive-skill 压缩：不常用分类折叠为计数行，常用分类保持完整可见
- 触发方式：直接运行 `python generic/cli.py demote --snapshot <snapshot.json> --usage usage.json --toolsets <当前工具集>`，或 `budget --input <index.txt>`
- 决策基于工具集映射 + 使用频率（近因衰减）；无工具集信息时不降级（安全网）
- 使用频率数据在 usage.json，自动记录，无需手动维护
```

## 2. USER.md 段（用户偏好，复制进 USER.md）

```markdown
- 用户的 skill 索引压缩：常用分类（devops/github/software-development 等）保持完整可见，低频分类折叠为计数行
- 配置可调参数在 core/config.py（list_budget_chars=4600、promote_score=2.0、decay_days=30）
```

## 3. MEMORY.md 段（环境事实，复制进 MEMORY.md）

```markdown
- progressive-skill：Hermes 插件（免费开源，GitHub/Gitee: freehul/progressive-skill）
  - 通用核心在 generic/（core/ + cli.py，零 Hermes 依赖，任何 agent 可用）
  - Hermes 适配层：__init__.py（自动挂钩，无需手动调用）
  - 使用频率数据：usage.json（插件目录下）
  - 官方 issue 参考：#22620（skill 索引压缩）
```

---

## 安装提示

1. 三个文件（SOUL.md / USER.md / MEMORY.md）在你 agent 的记忆目录（如 Hermes: `%LOCALAPPDATA%\hermes\`）
2. 复制对应段落到文件末尾即可
3. 删除时整段删除，不留残留
