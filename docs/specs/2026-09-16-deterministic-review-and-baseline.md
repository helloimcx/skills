# Spec: 确定性审查门禁与工程生产基线升级

## Goal

针对通用 AI 代码审查容易出现的“大改动漏看、行号漂移、缺乏覆盖约束、风格噪音过多”等问题，将工业级代码审查的核心工程哲学内化到本仓库中：
1. **升级 `vibe-coding` Review 门禁**：引入**确定性文件覆盖率清单（Coverage Checklist 100% 追踪）**、**关联文件分治打包审查（Divide-and-Conquer）**、**严格的 Linter 与 Reviewer 职责解耦**以及**结构化评级与高信噪比过滤**，完全通过内置规程自闭环，不依赖任何额外外部工具。
2. **升级 `project-setup` 生产基线**：在生产基线中确立 **代码审查规则治理（Code Review Governance）** 与 **确定性工程与模型解耦原则**，使新项目具备规范化的路径级审查规则资产与 CI 审查门禁标准。

## Scope

- 修改 `vibe-coding/references/review.md`
- 修改 `vibe-coding/SKILL.md`
- 修改 `project-setup/references/production-baseline.md`
- 修改 `project-setup/SKILL.md`
- 扩展 `tests/test_vibe_coding_contract.py`
- 扩展 `project-setup/tests/test_skill_contract.py`

## Non-goals

- 不依赖、不提及、不安装任何第三方特定品牌工具（完全使用现有工作流与标准工具链自闭环）。
- 不破坏现有架构治理、Archify、复杂度与重复率等静态质量硬门禁。

## Behavior / Interface

### 1. `vibe-coding` Review 细则
- **Coverage Checklist 契约**：Reviewer 必须为每一个变更文件建立并维护 `(path, status)` 跟踪清单，状态显式标记为 `[REVIEWED]` 或 `[SKIPPED(reason)]`。审查报告必须汇总 `total_files`、`reviewed_files`、`skipped_files`，达到 100% 显式覆盖，严禁静默遗漏。
- **关联文件分治打包原则**：变更文件数 > 5 或行数 > 300 时，禁止单次全量灌入模型。必须将关联文件（如接口与实现、数据模型与持久化、配置与消费点）打包为独立审查单元（Review Unit）分批审查，防止长上下文导致浅尝辄止。
- **职责解耦与降噪**：
  - 格式排版、导入排序、命名风格等已有 Linter / 编译器负责的规则，严禁列为阻塞 Finding；
  - Review 聚焦逻辑 Bug、并发竞态、边界异常、资源生命周期、安全隐患与架构一致性；
  - 采用结构化 Finding 模式（包含 path、line、category、severity、content、suggestion_code），默认过滤 Low 级别噪音，仅对 Critical / High 设卡。
- **业务上下文锚定**：将阶段 3 已批准的 Spec / Plan 作为审查上下文基准，审查代码与设计的一致性，防止过度设计。

### 2. `project-setup` 生产基线
- **代码审查规则资产化（Review Rules as Code）**：
  - 项目应将特定路径的审查红线沉淀为仓库资产（如规则配置文件或规范文档），让敏感边界（如公共 API 入口、数据库事务、鉴权中间件）具有针对性的审查约束。
- **确定性工程与模型解耦原则（Deterministic First）**：
  - 语法、格式、静态质量指标必须由确定性工具（Linter、AST 分析器、单测）保证；模型仅负责语义、业务逻辑与架构边界，严禁使用模型替代确定性工具。
- **CI 自动化审查门禁**：
  - 在 CI 流程中提供标准化的 PR 审查门禁规范与结构化卡点支持。

## Acceptance Criteria

1. `vibe-coding/SKILL.md` 和 `vibe-coding/references/review.md` 明确包含 Coverage Checklist 100% 覆盖追踪、分治打包、Linter 与 Reviewer 职责解耦等规范。
2. `project-setup/SKILL.md` 和 `project-setup/references/production-baseline.md` 明确包含代码审查规则治理、确定性工具解耦与 CI 审查门禁等规范。
3. 文档与规则中不依赖任何外部专用审查工具，保持自闭环。
4. `tests/test_vibe_coding_contract.py` 与 `project-setup/tests/test_skill_contract.py` 增加对应契约测试并通过。
5. 全量自动化测试（`tests/` 和 `project-setup/tests/`）100% 通过。
