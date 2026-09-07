# Architecture & Documentation Synchronization

代码是系统实现的事实来源，文档必须持续描述当前代码库的真实状态。

## 执行时机与输入

在实现和全量测试完成后、独立 Review 前执行。比较当前工作区与创建任务分支时使用的目标分支（通常为 `origin/main` 或 `origin/master`）的完整 Git Diff，覆盖已提交、已暂存和未暂存改动，不得直接跳过。

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

只更新实际受影响的文档：

- 系统整体架构与分层矩阵 → `docs/architecture/`（更新对应的 L1 `system-architecture.json`、L2 `*.workflow.json` / `*.sequence.json`、L3 `*.lifecycle.json` 规范）
- 语义架构变更记录与对比图 → `docs/architecture/changes/YYYY-MM-DD-<task-slug>.md` 与方案阶段成对生成的 `YYYY-MM-DD-<task-slug>.html`（Delta 对比画布）
- 全景矩阵大盘与 README → `docs/architecture/overview.md` 与根目录 README 中的 `<picture>` managed block
- 模块职责与边界 → `docs/architecture/modules.md`
- 新增职责与边界清晰的较大独立模块 → `docs/architecture/modules/<module-name>.md`，并在 `docs/architecture/modules.md` 增加入口
- 模块依赖关系 → `docs/architecture/dependencies.md`
- 重要架构决策及原因 → `docs/architecture/decisions/` 中的 ADR
- 功能规格变化 → 对应 Spec
- 开发计划变化 → 对应 Plan
- Agent 工作规则、开发命令或仓库级约束变化 → `CLAUDE.md` / `AGENTS.md`

`CLAUDE.md` 和 `AGENTS.md` 只维护稳定的仓库级规则与文档入口，不复制容易变化的详细架构内容。

当模块具有独立业务职责，并拥有公共接口、独立依赖边界或核心数据流之一时，按较大独立模块处理。独立模块文档使用小写 kebab-case 模块名，至少说明模块目标、职责与边界、公共接口、关键依赖或数据流；当结构、依赖或流程仅靠文字不易理解时，优先使用 Archify 交付交互 HTML 画布（或在 fallback 时使用 Mermaid/ASCII 示意图）。

## 3. 一致性原则

- `docs/architecture/` 描述当前实现的真实架构，提交完成后必须与当前 HEAD 一致，不保留未标记的失效描述；
- 架构变更时必须执行自动化架构门禁（如 `lint:arch`），确保所有规范符合 Schema 且通过 Showcase 检查（0 错误、0 警告）；
- 历史架构决策及原因通过 ADR 保存；
- 语义变更历史与对比产物由 `docs/architecture/changes/` 保存，代码变更历史由 Git 保存；
- 文档名称、路径和术语与代码中的模块、接口和数据结构保持一致；
- 如果分析或文档同步暴露实现问题并引发代码修改，重新运行完整测试套件和本分析。

## 4. Review Gate

Reviewer 必须同时检查：

`Code Diff + Architecture Diff + Documentation Diff`

若代码改变模块边界、依赖关系、公共接口、核心流程、数据流、API、配置、部署拓扑或开发/构建/测试流程，而相关文档没有同步更新，Review 不得通过。新增较大独立模块却没有对应模块文档，也视为阻塞问题。

若任务涉及架构变更（`Architecture Impact: Required`），Reviewer 必须验证最终代码实现是否与已批准的 `docs/architecture/changes/YYYY-MM-DD-<task-slug>.html` 方案对比图一致，且 `lint:arch` 门禁结果为 0 错误 0 警告。

文档影响分析与必要的文档更新属于 Definition of Done。


方案图格式遵循 [SKILL.md](../SKILL.md) 的分级规则：中等且无架构变化的任务仅需在 Plan 中嵌入简要 Mermaid，不要求 HTML 或架构变更语义记录；复杂任务继续使用 `/archify`。已声明 Archify 降级时，上述 HTML 一致性与成对归档检查改为检查 Plan 中的回退图、降级原因及语义记录引用。
