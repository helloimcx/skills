# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

This is a **skills project collection** (技能项目合集) for creating and managing Claude Code skills.

## Development Workflow

**IMPORTANT:** This is a development workspace. Follow this workflow when creating or modifying skills:

1. **Create/Modify** - Create or edit skill files in the current repository directory
2. **Review** - Wait for user confirmation before proceeding
3. **Install** - Only after user confirms, install the skill to Claude

Do NOT install skills to Claude until the user explicitly confirms approval.

## Repository Tests and CI

- 开发与缺陷修复优先运行修改点和相关模块测试；影响范围不明确或风险较高时扩大验证范围。
- 根目录契约入口：`python3 -m unittest discover -s tests -p 'test_*.py'`。
- code-loop 服务完整入口：`python3 -m unittest discover -s services/code-loop/tests -p 'test_*.py'`。
- GitHub Actions 在 PR、main push 和手动触发时并行执行两套入口（Python 3.9 / 3.13）；成功汇总检查名为 `CI Gate`。
- code-loop Handler 先做定向回归，再按目标仓库 base commit 考题配置执行快速层或回退到完整层；Reviewer 始终跑完整验证。
- `CI Gate` 只有在目标仓库配置为 required check 后才会阻止不通过的合并。

## Installed Skills

Skills are installed by copying folders directly to `~/.claude/skills/`.

### project-setup
- **目录**: `project-setup/`
- **功能**: 独立、安全地初始化具备可持续架构治理、Codex/Claude Code 共享规则、replaceable diagram provider、测试体系与硬静态质量门禁、Docs as Code、可观测性和安全默认值的新软件项目
- **文件**: `project-setup/SKILL.md`

### vibe-coding
- **目录**: `vibe-coding/`
- **功能**: 面向高质量软件开发的端到端 Vibe Coding 工作流（需求澄清/复杂度分级/并行规划/TDD/Review/真实QA闭环）
- **文件**: `vibe-coding/SKILL.md`

### prototype-first
- **目录**: `prototype-first/`
- **功能**: 原型先行 UI 开发流程（在项目 design/ 产出高保真可点击原型，经确认后才允许动实现代码）
- **文件**: `prototype-first/SKILL.md`

### code-duplication-scanner
- **目录**: `code-duplication-scanner/`
- **功能**: 检测 Git 修改代码与现有代码库之间的重复代码
- **文件**: `code-duplication-scanner/SKILL.md`

### github-arch-analyzer
- **目录**: `github-arch-analyzer/`
- **功能**: 分析 GitHub 项目架构，包括功能、技术栈、系统设计和关键模块
- **输出**: 中文技术文档，必要时调用 d2-diagrams skill 生成可视化图表
- **文件**: `github-arch-analyzer/SKILL.md`

### code-loop
- **目录**: `code-loop/`
- **功能**: 本地确定性自动化研发闭环（Control Plane 协调器、GitHub 审查与合并调度、门禁修复、Operator 监控自治与 Dashboard）
- **文件**: `code-loop/SKILL.md`
- **参考契约**: `code-loop/references/`（`control-plane.md`、`reviewer.md`、`handler.md`、`discover.md`、`operator.md`）
- **委托 CLI**: `code-loop/scripts/`（`control_plane.py`、`dashboard.py`、`trigger.py`、`cp_server.py`、`worker_agent.py`、`operator.py`、`check_ledger.py`）


### d2 (D2 Diagramming)
- **目录**: `d2/`
- **功能**: D2 图表绘制工具 - 流程图、架构图、UML 类图、ERD、序列图

## Integrated Services

### code-loop
- **目录**: `services/code-loop/`
- **功能**: 本地确定性运行时与自动化评审调度系统（纯 Python 3.9+ 标准库与 SQLite，零外部依赖）
- **架构与模块**:
  - `codeloop/cp/`: Control Plane 协调器核心（状态空间、租约控制、原子转移、考题护栏）
  - `codeloop/cp/github.py`: 原生 GitHub REST API (v2022-11-28) 适配器，支持 `GITHUB_TOKEN`、Bearer 认证、`gh` CLI 与 `codeloop.cp.gitea` 向后兼容别名
  - `codeloop/dash/` & `frontend/`: 监控面板后台与纯静态前端（实时会话流、行动卡片、决策回复）
  - `scripts/cp_server.py`: 合并服务（8722 协调面 + 127.0.0.1:8712 面板 UI）
  - `scripts/worker_agent.py`: 常驻 Worker Agent（RFC6455 WebSocket 指令通道）

## D2 Skill Architecture

The D2 diagramming skill is organized as follows:

- `d2/SKILL.md` - Main entry point with critical syntax rules and quick reference
- `d2/d2.skill` - Binary skill file for Claude Code integration
- `d2/CLASSES.md` - UML class diagram syntax (fields, methods, visibility, relationships)
- `d2/CLASSES_REUSE.md` - CSS-like class system for reusable styles
- `d2/SQL_TABLES.md` - Database/ERD diagram syntax with constraints (PK, FK, UNQ, NN)
- `d2/STYLES.md` - Complete style properties reference
- `d2/IMAGES.md` - Icon and image embedding syntax
- `d2/EXAMPLES.md` - Common diagram patterns (flowcharts, architecture, state machines)
- `d2/TROUBLESHOOTING.md` - Common errors and solutions
- `d2/assets/examples/` - Example D2 files (flowchart, architecture, class-diagram, erd)

## Critical D2 Syntax Rules

When working with D2 code, always follow these rules to avoid "missing value after colon" errors:

### Hyphenated Properties Must Use Style Blocks

**WRONG:**
```d2
node.style.stroke-width: 2     # ERROR
node.style.stroke-dash: 3       # ERROR
node.style.border-radius: 5     # ERROR
```

**CORRECT:**
```d2
node.style: {
  stroke-width: 2
  stroke-dash: 3
  border-radius: 5
}
```

Dot notation only works for non-hyphenated properties:
```d2
node.style.fill: red            # OK
node.shape: circle              # OK
```

### Valid Numeric Property Ranges

| Property | Valid Range |
|----------|-------------|
| stroke-width | 1-15 |
| stroke-dash | 0-10 |
| border-radius | 0-20 |
| font-size | 8-100 |
| opacity | 0-100 |

### Multiple Classes Use Semicolons

```d2
node.class: [class1; class2]    # Use semicolons, not commas
```

## Common D2 Patterns

### Flowchart Structure
```
direction: down
Start: { shape: oval; style: { fill: #c8e6c9 } }
Process: { shape: rectangle }
Decision: { shape: diamond; style: { fill: #fff9c4 } }
End: { shape: oval; style: { fill: #ffcdd2 } }
```

### UML Class Relationships
```
Child ->> Parent: extends           # Inheritance
Interface ..|> Implementation       # Implementation
A ->* B                             # Composition
A o-> B                             # Aggregation
A ..> B                             # Dependency
```

### SQL Table Constraints
```
table_name: sql_table {
  id: int PK                        # Primary Key
  email: string UNQ NN              # Unique + Not Null
  user_id: int FK                   # Foreign Key
}
```

## Container Syntax

```d2
# Named container
container "My Group": {
  A
  B
}

# Nested containers
outer: {
  inner: {
    C
  }
}
```

## Direction Control

```d2
direction: down     # Top to bottom flow
direction: right    # Left to right flow
```
