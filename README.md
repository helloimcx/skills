# Skills Collection

This repository contains a collection of Agent Skills compatible with [skills.sh](https://skills.sh) and AI coding agents (Claude Code, Cursor, Windsurf, Antigravity, Codex, etc.).

## Skills Overview

| Skill | Description | Installation |
|---|---|---|
| `vibe-coding` | 端到端高质量 Vibe Coding 工作流（需求澄清/复杂度分级/并行规划/TDD/Review/真实QA闭环） | `npx skills add helloimcx/skills --skill vibe-coding` |
| `project-setup` | 安全初始化具备可持续架构治理、跨 Agent 规则、测试体系与硬静态质量门禁、文档和真实验证的新软件项目 | `npx skills add helloimcx/skills --skill project-setup` |
| `prototype-first` | 原型先行 UI 开发流程（Apple HIG 品质、直观交互、截图自问自审，经确认后才动实现代码） | `npx skills add helloimcx/skills --skill prototype-first` |
| `code-duplication-scanner` | 检测 Git 修改代码与现有代码库之间的重复代码 | `npx skills add helloimcx/skills --skill code-duplication-scanner` |
| `code-loop` | 本地确定性自动化研发闭环（Control Plane 协调器、GitHub 审查与合并调度、门禁修复、Operator 监控自治与 Dashboard） | `npx skills add helloimcx/skills --skill code-loop` |
| `d2-diagrams` | 根据自然语言描述生成 D2 架构图、流程图、ERD、UML 类图与序列图 | `npx skills add helloimcx/skills --skill d2-diagrams` |
| `github-arch-analyzer` | 分析 GitHub 项目架构并输出结构化中文技术文档 | `npx skills add helloimcx/skills --skill github-arch-analyzer` |

## Services Overview

| Service | Path | Description |
|---|---|---|
| `code-loop` | `services/code-loop/` | 本地确定性控制面与自动化评审调度服务（Control Plane 协调器、Dashboard 监控面板、GitHub 适配集成、Worker Agent 指令通道） |

## Installation & Usage

### Via skills.sh CLI (`npx skills`)

Install all skills in this repository:
```bash
npx skills add helloimcx/skills --all
```

Install a specific skill (e.g. `vibe-coding`):
```bash
npx skills add helloimcx/skills --skill vibe-coding
```

Install globally across all supported agents:
```bash
npx skills add helloimcx/skills --skill vibe-coding -g
```

### Manual Installation

Copy the desired skill directory to your agent's skill directory:
- Claude Code: `~/.claude/skills/<skill-name>/`
- Codex: `~/.codex/skills/<skill-name>/`
- Antigravity / Gemini: `~/.gemini/config/skills/<skill-name>/`
