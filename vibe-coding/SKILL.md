---
name: vibe-coding
description: 面向实现功能、修复 Bug、重构和技术改造的端到端编码工作流。先检查仓库并按风险分级，再通过方案或 Spec/Plan 获得批准，以 TDD 实现，并完成独立 Review 与风险适配的真实 QA；中等/复杂任务将最终 Spec 和 Plan 持久化到 docs/。
---

# Vibe Coding

目标：以尽可能少的沟通成本，把需求稳定地变成**正确、简洁、可维护、经过真实验证**的代码。

`理解需求 → 检查仓库 → 必要时澄清 → 复杂度分级 → (中等/复杂任务创建分支与 Worktree) → 方案/Spec+Plan (含 HTML 对比图/方案图) → 用户批准 → TDD 实现 → 架构与文档同步 (lint:arch) → 独立 Review → 真实 QA → 完成`

## 0. 核心原则

1. **先读代码，再提问题。** 仓库能够回答的问题不要问用户。
2. **只问阻塞问题。** 仅询问会显著改变行为、接口、数据或架构的歧义。
3. **按风险分级。** 文件数量只是参考；任一高风险维度都可以升级复杂度。
4. **编码前只有一个 Approval Gate。** 批准后自主执行到 Review 和 QA 完成，除非出现重大新风险、不可逆操作或需求矛盾。
5. **默认隔离开发。** 中等和复杂任务在方案设计前即从最新 `origin/main` 或 `origin/master` 创建任务分支和独立 Git worktree，所有方案分析、规范草案、对比图与代码均在隔离沙箱中生成；简单任务在获批后或在隔离分支中执行。
6. **复杂任务先并行规划，再集中裁决。** 3 个相同角色的独立 Subagent 分别求解，主 Agent 形成唯一最终方案。
7. **TDD 是默认实现方式。** 遵循 `RED → GREEN → REFACTOR`；全部行为切片完成后，完整测试套件必须全部通过。
8. **Review 和 QA 是质量门禁。** 验证深度按任务风险和项目能力选择，发现有效问题必须修复并重测。
9. **文档必须反映当前代码。** 中等/复杂任务方案图使用 `archify compare` 或 `deliver` 生成交互式 HTML（存储于 `docs/architecture/changes/`），替换随意文本草图；实现完成后、Review 前必须通过 `lint:arch` 门禁并同步更新正式架构规范。
10. **优先最简单的正确方案。** 尊重现有架构，避免无需求支撑的抽象、框架和顺手重构。
11. **Done 需要证据。** 相关验收标准、自动化测试和适用的真实主路径均有验证结论。

---

## 1. 理解需求与检查仓库

在提问或设计方案前：

1. 阅读适用的 `AGENTS.md`、`CLAUDE.md`、`CONTRIBUTING.md`、README 和架构文档。
2. 定位入口、调用链、核心模块、数据模型、配置和现有测试。
3. 搜索相似实现，确认构建、测试、Lint、类型检查和本地启动方式。
4. 对 Bug，先理解或复现现象，再判断根因。

只读取足够支撑当前决策的上下文，不漫无目的浏览整个仓库。

仅在仓库无法回答且会改变实现方案时提问，例如兼容性、迁移许可、业务规则、权限边界或外部约束。不要询问命名、文件位置、helper 选择、测试文件名等内部实现细节。需要澄清时，一次提出少量高价值问题。

---

## 2. 复杂度与编码前方案

依据 [references/complexity.md](references/complexity.md) 判定为**简单 / 中等 / 复杂**，内部用一句话记录判定理由。

### 方案表达规则

所有面向用户的方案必须**结论先行、简洁、清晰、易懂**：使用短标题和要点，只保留会影响用户决策、实现或验收的信息，避免重复仓库调查过程和堆砌术语。

中等和复杂任务必须遵循以下规则：
1. **沙箱隔离先行：** 判定为中等或复杂任务后，默认先获取 `origin` 最新状态，从实际存在的最新 `origin/main` 或 `origin/master` 创建任务分支与独立 `git worktree`（如 `task/YYYY-MM-DD-<task-slug>`），确保宿主工作区绝对纯净，所有方案分析、Spec、Plan 及图表均在 worktree 内生成。
2. **Archify 方案图/对比图（替换原有纯文本草图）：** 方案必须附带一张可视化方案图，优先利用 Archify 生成高保真交互式 HTML 画布（或染色对比图），禁止使用随意的手绘文本草图：
   - **架构/模块演进（Architecture Impact: Required）：** 调用 `archify compare architecture <base.json> <candidate.json> docs/architecture/changes/YYYY-MM-DD-<task-slug>.html` 生成 Before / Delta / After 染色对比图，直观呈现组件变更、依赖流转与边界差异；
   - **业务流/时序/状态机新增或改造：** 调用 `archify deliver workflow|sequence|lifecycle <candidate.json> docs/architecture/changes/YYYY-MM-DD-<task-slug>.html --quality showcase` 生成高分辨率交互画布；
   - **优雅降级（Fallback）：** 若环境未安装 Archify CLI 或属于未接入 AaC 的传统项目，明确标注 `[FALLBACK: mermaid]` 或 `[FALLBACK: ascii]`，使用紧凑 Mermaid/ASCII 示意图作为回退；
   - **持久化位置：** 统一存储在 **`docs/architecture/changes/YYYY-MM-DD-<task-slug>.html`**（配合可选的 `*.candidate.json`），与该任务的架构变更语义记录成对归档，供用户在 Approval Gate 前在浏览器直观探索；
3. **Plan 引用：** 在 `docs/plans/YYYY-MM-DD-<task-slug>.md` 中直接以相对路径引用此 HTML 对比图，作为后续 TDD 实施与 Review 的权威视觉承诺；
4. **方案图 vs 汇报图：** 方案阶段的图是**“设计预期与变更承诺 (Expected / Delta)”**；任务完成时仍需生成反映实际代码落地的**“交付图 (Actual As-Built)”**。

### 简单任务

向用户给出简洁方案摘要，说明问题或根因、修改方式和关键测试，然后等待批准。不要制造永久 Spec 文档。

### 中等任务

提交可执行的 **Spec + Plan**，并附带生成的 HTML 方案图/对比图路径，然后等待批准：

- Spec：Goal、Scope、Non-goals、Behavior / Interface、Constraints / Compatibility、Acceptance Criteria。
- Plan：有顺序的修改步骤、涉及模块或文件、数据 / API / 配置变化、测试策略、验证顺序、方案图链接。

### 复杂任务

读取并执行 [references/parallel-planning.md](references/parallel-planning.md)：并行召唤 3 个接收相同任务与核心上下文的独立 Subagent，由主 Agent 比较正确性、简洁性、架构契合度、可维护性、兼容性、风险、可测试性和完整性。

主 Agent 只向用户提交一个最终权威版 Spec + Plan，附生成的 HTML 方案图/对比图路径，并简要说明关键取舍，然后等待批准。

所有 Acceptance Criteria 必须可验证，避免“功能正常”“性能良好”等不可执行表述。

---

## 3. Approval Gate 与文档持久化

用户批准方案后：

1. 将最终 Spec、Acceptance Criteria 及已批准的方案对比图作为实现基线。
2. 自主完成编码、测试、Review、修复和 QA，不因普通实现细节再次请求确认。
3. 不擅自扩大 Scope。若出现重大方案错误、数据丢失风险、不可逆操作、安全风险或必须改变已批准行为，停止受影响部分并说明。

### 隔离工作区

中等和复杂任务在方案阶段已建立独立任务分支与 `git worktree`；用户批准后，直接在已建立的 worktree 内进入 TDD 编码阶段。若方案被用户否决或任务放弃，直接移除该 worktree，宿主分支零污染。

仅当仓库没有 Git/对应远端分支、环境不支持 worktree 或用户明确要求时例外，并简要说明原因。

### Spec / Plan 持久化契约

中等和复杂任务必须遵守：

1. **时机：** 用户批准前在 worktree 内生成最终版本，批准后作为只读实施基线；聊天消息或临时计划工具不能代替仓库文档。
2. **位置：** Markdown 文档位于仓库根目录的 `docs/` 下，方案 HTML 对比图位于 `docs/architecture/changes/` 下。
3. **命名：** 固定使用 `docs/specs/YYYY-MM-DD-<task-slug>.md`、`docs/plans/YYYY-MM-DD-<task-slug>.md` 和 `docs/architecture/changes/YYYY-MM-DD-<task-slug>.html`。日期取首次持久化时的本地日期；文档使用相同日期与小写 kebab-case task slug，后续更新不得改名。
4. **创建：** 目标目录不存在时创建；不得覆盖无关文档。恢复已有任务时优先更新现有文件，不创建重复版本。
5. **同步：** 已批准的 Scope、行为、接口、Acceptance Criteria 或实施步骤发生必要变更时，先同步对应文档与图表，再继续受影响的实现。

Spec 和 Plan 必须自包含，并保留上一节规定的必要字段与方案图引用。

---

## 4. TDD 实现

对每个最小行为切片执行：

1. **RED：** 编写能证明目标行为的测试，并确认它因能力尚未实现而失败。
2. **GREEN：** 编写使当前测试通过的最小正确实现。
3. **REFACTOR：** 保持测试绿色，清理重复、坏命名、过深嵌套和不必要复杂度。

按需求选择 Unit、Integration、Contract/API、E2E 或 Behavior Tests。禁止通过削弱、删除或篡改有效测试让实现通过；如果测试与已批准 Spec 冲突，先判断哪一方过时。

复杂任务可并行实现边界清晰的独立模块；每个写入子任务使用独立 worktree，避免同时修改同一核心文件。主 Agent 负责最终集成，并在集成后重新运行完整相关测试。

### 全量测试门禁

完成全部 TDD 行为切片后、进入 Review 前，运行仓库定义的**完整自动化测试套件**，而不只是修改点相关测试。只有全部测试通过才可以进入 Review。

任何失败都必须区分为本次回归、已存在失败或环境故障，并尽可能修复或排除。完整套件未全部通过或无法执行时，标记为 `[BLOCKED]` 并说明原因；不得绕过门禁或宣布完成。

---

## 5. 架构与文档同步

代码是系统实现的事实来源。实现和全量测试完成后、进入 Review 前，读取并执行 [references/documentation-sync.md](references/documentation-sync.md)：基于当前工作区与目标分支的完整 Git Diff 检查架构和文档影响，不得跳过。

- **若判定为 `Architecture Impact: Required`：**
  1. 更新 `docs/architecture/` 下的正式 L1-L3 规范（`system-architecture.json`、`*.workflow.json`、`*.sequence.json`、`*.lifecycle.json`）；
  2. 完善 `docs/architecture/changes/YYYY-MM-DD-<task-slug>.md` 语义变更记录，与方案阶段生成的 `YYYY-MM-DD-<task-slug>.html` 对比图形成成对归档；
  3. 执行项目架构防腐门禁（如 `lint:arch`），必须通过 Schema 与 Showcase 质量检查（0 错误、0 警告）；
  4. 同步更新 `docs/architecture/overview.md` 全景矩阵大盘与 README managed block。
- **若无架构与文档影响：** 明确记录 `Docs Impact: None`。
- 新增职责与边界清晰的较大独立模块时，创建对应模块架构文档并按需配图；
- `docs/architecture/` 描述当前代码的真实架构，历史决策由 ADR 保存，变更历史由 `changes/` 与 Git 保存。

文档同步属于 Definition of Done。若分析过程引发代码修改，重新执行全量测试和本步骤。

---

## 6. 独立代码 Review

架构与文档同步完成后，召唤 1 个未参与实现的独立 Subagent，按 [references/review.md](references/review.md) 只读检查最终 Spec、Acceptance Criteria、Code Diff、Architecture Diff、Documentation Diff 和必要上下文：

- 简单任务：Adversarial Review；
- 中等任务：Spec Verifier + Cleaner；
- 复杂任务：再增加 Architect Review（重点验证实现是否与已批准的 `docs/architecture/changes/YYYY-MM-DD-<task-slug>.html` 方案图/对比图一致，以及 L1-L3 规范和 `lint:arch` 门禁状态）。

主 Agent 验证每条 Finding，不机械接受意见。有效问题进入：

`Review → 修复 → 相关测试 → 必要时补回归测试 → 再 Review`

直到没有未解决的阻塞问题。

---

## 7. QA 与真实验证

Review 通过后，读取 [references/qa.md](references/qa.md)，按**任务风险、改动边界和项目实际能力**选择适用检查。上一节的完整自动化测试套件是硬门禁；除此之外，不机械要求每个任务执行不存在的测试层级或启动不存在的服务。

最低验证基线：

1. 执行项目存在且与改动相关的 Build、Lint、Format、Type Check 以及架构门禁（如 `lint:arch`）。
2. 确认 TDD 完成后的完整自动化测试套件已全部通过。
3. 改动跨越模块、公共接口或持久化边界时，增加适用的 Integration、Contract/API 或 E2E 测试。
4. 项目存在可运行的真实入口且环境允许时，启动应用或服务，通过正式入口验证至少一条相关主路径。
5. 对照 Acceptance Criteria 逐项记录验证证据。

不适用的检查可以跳过；因环境限制无法执行的检查标记为 `[BLOCKED]` 并说明原因。未执行的检查不得标记为 `[PASS]`。

QA 失败时执行：

`最小化复现 → 判断根因 → 补测试 → 修复 → 必要时再 Review → 重跑 QA`

直到通过或明确被外部环境阻塞。

---

## 8. 完成标准

只有同时满足以下条件才可以宣布完成：

- 已批准的 Scope 已实现；
- Acceptance Criteria 有明确验证结论；
- 仓库定义的完整自动化测试套件全部通过；
- 已记录文档影响分析，所有受影响架构（含 L1-L3 规范及 `lint:arch` 门禁）与文档已同步；
- Review 无未解决阻塞问题；
- 适用的真实主路径已验证，或明确说明环境限制；
- 没有隐藏已知失败、无关改动或明显临时代码。

最终汇报保持简洁：说明实现内容、关键设计决定、测试与 QA 结果，以及仍存在的外部限制或风险。不要重复整份 Spec 或输出冗长工作日志。

任务完成后，最终汇报必须附一张基于最终代码生成的真实**交付架构/流程图**（优先展示正式更新的 HTML 画布相对路径或双主题图片链接，未配置 Archify 时使用紧凑 Mermaid/ASCII 示意图），聚焦展示改动入口、实际落地改动点及其关系或影响，证明真实落地成果与最初方案承诺的一致性。

