# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

This is a **skills project collection** (技能项目合集) for creating and managing Claude Code skills.

## Development Workflow

**IMPORTANT:** This is a development workspace. Follow this workflow when creating or modifying skills:

1. **Create/Modify** - Create or edit skill files in the current directory (`/Users/mochuxian/code/skills/`)
2. **Review** - Wait for user confirmation before proceeding
3. **Install** - Only after user confirms, install the skill to Claude

Do NOT install skills to Claude until the user explicitly confirms approval.

## Installed Skills

Skills are installed by copying folders directly to `~/.claude/skills/`.

### vibe-coding
- **目录**: `vibe-coding/`
- **功能**: 面向高质量软件开发的端到端 Vibe Coding 工作流（需求澄清/复杂度分级/并行规划/TDD/Review/真实QA闭环）
- **文件**: `vibe-coding/SKILL.md`

### code-duplication-scanner
- **目录**: `code-duplication-scanner/`
- **功能**: 检测 Git 修改代码与现有代码库之间的重复代码
- **文件**: `code-duplication-scanner/SKILL.md`

### github-arch-analyzer
- **目录**: `github-arch-analyzer/`
- **功能**: 分析 GitHub 项目架构，包括功能、技术栈、系统设计和关键模块
- **输出**: 中文技术文档，必要时调用 d2-diagrams skill 生成可视化图表
- **文件**: `github-arch-analyzer/SKILL.md`


### d2 (D2 Diagramming)
- **目录**: `d2/`
- **功能**: D2 图表绘制工具 - 流程图、架构图、UML 类图、ERD、序列图

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
