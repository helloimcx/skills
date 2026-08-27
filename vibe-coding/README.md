# Vibe Coding Skill

一套面向高质量 Agent Coding 的端到端开发 Skill。

## 主要特性

- Inspect before asking：先读代码再 Grill；
- 简单 / 中等 / 复杂三级流程；
- 复杂任务：3 个相同角色的独立 Subagent 并行规划，主 Agent 统一评判和综合；
- 编码前唯一 Approval Gate；
- 中等/复杂任务：批准后的 Spec 和 Plan 以两个 Markdown 文档持久化到 `docs/`；
- TDD：RED → GREEN → REFACTOR；
- 独立 Subagent Review；
- Review / QA 失败自动回流；
- 按任务风险执行适用的自动化测试与真实主路径验证。

## 目录

```text
vibe-coding/
├── SKILL.md
├── README.md
├── agents/
│   └── openai.yaml
└── references/
    ├── complexity.md
    ├── parallel-planning.md
    ├── review.md
    └── qa.md
```

## 使用

### Codex

将目录放到 Codex Skills 目录中，例如：

```text
~/.codex/skills/vibe-coding/
```

或按你的项目级 Skills 目录约定安装。

### Claude / Agent Skills

将整个目录作为一个 Skill 安装，核心入口为 `SKILL.md`。

不同 Harness 的 Subagent / Worktree / Shell 工具名称可能不同，Skill 使用的是能力语义，不绑定某个具体调用命令。
