# Architecture & Documentation Synchronization

代码是系统实现的事实来源，文档必须持续描述当前代码库的真实状态。

## 执行时机与输入

在实现完成后、独立 Review 前执行。比较当前工作区与创建任务分支时使用的目标分支（通常为 `origin/main` 或 `origin/master`）的完整 Git Diff，覆盖已提交、已暂存和未暂存改动，不得直接跳过。

将结论记录到 Review 输入；存在 Plan 时也同步写入 Plan 的文档影响部分：

- 无影响：`Docs Impact: None`
- 有影响：列出需要更新或已更新的文档及原因

## 1. Architecture & Documentation Impact Analysis

检查是否发生：

- 新增、删除、重命名或移动模块；
- 模块职责或公共接口变化；
- 模块依赖新增、删除或方向变化；
- 分层结构变化；
- 数据模型或核心数据流变化；
- API、配置结构或部署拓扑变化；
- 开发、构建或测试流程变化。

即使判断没有影响，也必须完成检查并记录 `Docs Impact: None`。

## 2. 文档更新映射

先检查目标仓库实际存在的架构资产、维护规则、schema 和检查命令，再只更新实际受影响且由项目契约要求的文档：

- 系统整体架构、分层和流程 → 项目实际的权威架构文档；若项目契约采用 JSON L1-L3，则按其 schema 更新 `system-architecture.json`、`*.workflow.json`、`*.sequence.json`、`*.lifecycle.json` 等相应规范
- 语义架构变更记录与对比图 → 项目已有的变更记录位置与格式，引用 Plan 中的 Mermaid/ASCII
- 全景矩阵、模块职责、依赖和索引 → 项目已有的 overview（如 `overview.md`）、模块、依赖文档及 README 入口
- 新增职责与边界清晰的较大独立模块 → 项目约定的模块文档位置，并在其索引中增加入口
- 重要架构决策及原因 → 项目已有的 ADR 位置和格式
- 功能规格变化 → 对应 Spec
- 开发计划变化 → 对应 Plan
- Agent 工作规则、开发命令或仓库级约束变化 → `CLAUDE.md` / `AGENTS.md`

`CLAUDE.md` 和 `AGENTS.md` 只维护稳定的仓库级规则与文档入口，不复制容易变化的详细架构内容。

当模块具有独立业务职责，并拥有公共接口、独立依赖边界或核心数据流之一时，按较大独立模块处理。独立模块文档使用小写 kebab-case 模块名，至少说明模块目标、职责与边界、公共接口、关键依赖或数据流；当结构、依赖或流程仅靠文字不易理解时，使用紧凑 Mermaid/ASCII 示意图。

## 3. 一致性原则

- 先确认目标仓库实际存在的权威文档、维护规则、schema 和可用检查；架构同步遵循项目已有契约，不以本 Skill 预设某一种文件格式；
- Markdown-only 项目更新其现有权威 Markdown 文档，不创建项目没有的 JSON schema、`lint:arch` 命令或新项目脚手架；
- 只有项目契约确实要求 JSON L1-L3 时才按对应 schema 更新；只有 `lint:arch` 实际可用且适用时才运行；
- 对项目契约没有要求的资产或检查，记录 `[N/A]` 和理由；契约要求执行但当前环境不可运行时记录 `[BLOCKED]` 和原因，不得标成 `[PASS]`；
- 项目的权威架构文档描述当前实现，提交完成后必须与当前 HEAD 一致，不保留未标记的失效描述；
- `project-setup` 的严格架构资产要求面向新项目初始化，不强制施加于存量项目；存量项目继续遵循自身架构契约；
- 历史架构决策及原因通过 ADR 保存；
- 语义变更历史与对比产物由 `docs/architecture/changes/` 保存，代码变更历史由 Git 保存；
- 文档名称、路径和术语与代码中的模块、接口和数据结构保持一致；
- 如果分析或文档同步暴露实现问题并引发代码修改，重新运行受影响的测试和本分析。

## 4. Review Gate

Reviewer 必须同时检查：

`Code Diff + Architecture Diff + Documentation Diff`

若代码改变模块边界、依赖关系、公共接口、核心流程、数据流、API、配置、部署拓扑或开发/构建/测试流程，而相关文档没有同步更新，Review 不得通过。新增较大独立模块却没有对应模块文档，也视为阻塞问题。

若任务涉及架构变更（`Architecture Impact: Required`），Reviewer 必须验证最终代码实现是否与已批准的方案图一致（验证 Plan 中的 Mermaid/ASCII），且项目已有且适用的架构门禁通过。

文档影响分析与必要的文档更新属于 Definition of Done。


方案图格式遵循 [SKILL.md](../SKILL.md)：方案图使用紧凑 Mermaid/ASCII；中等和复杂任务将其嵌入 Plan，架构变更语义记录引用该方案图。
