# Review 细则

Review 目标是找出作者会愿意修复的具体问题，而不是证明 Reviewer 聪明。

## Reviewer 通用规则

- 使用一个独立 Subagent；
- Reviewer 不参与本轮实现；
- Review 时只读，不修改代码；
- 读取最终 Spec / Acceptance Criteria 作为业务上下文锚点；
- 读取 Architecture & Documentation Impact Analysis；
- 同时检查 Code Diff、Architecture Diff、Documentation Diff，以及必要的上下文、调用方和测试；
- 继续检查完整改动，不因找到第一个问题而停止；
- 只报告具体、可验证、可行动的问题；
- 区分阻塞问题和建议，不把个人风格偏好升级成阻塞问题。

### 覆盖率清单与分治契约（Coverage & Divide-and-Conquer）

- **覆盖率清单（Coverage Checklist）**：Reviewer 必须为每一个待审查的变更文件建立 `(path, status)` 追踪清单，状态标记为 `[REVIEWED]` 或 `[SKIPPED(reason)]`。审查报告必须汇总 `total_files`、`reviewed_files`、`skipped_files`，实现 **100% 显式覆盖闭环**，严禁静默忽略任何文件；任何跳过的文件必须记录具体理由（如纯自动生成代码、锁定依赖更新等）。
- **超大变更分治（Divide-and-Conquer）**：变更文件数 <= 20 个时，默认进行全貌统一审查，以保证跨文件调用链与系统上下文的完整性；只有当变更文件数超过 20 个时，才按模块或子系统边界打包为相对独立的**审查单元（Review Unit）**分批审查，避免上下文过载导致注意力稀释，杜绝在常规任务中机械拆碎协同改动。

### Linter 与 Reviewer 职责边界（Strict Separation of Concerns）

- **Linter 工具先行**：代码排版、格式化（Formatting）、导入排序（Import Ordering）、简单命名风格等已有 Linter、Formatter 或编译器负责的事项，**严禁作为 Review 的阻塞 Finding**；
- **审查聚焦深度缺陷**：Reviewer 将注意力聚焦于静态工具无法覆盖的深层问题：业务逻辑缺陷、并发竞态、边界异常路径、资源与 Goroutine/连接生命周期泄漏、安全漏洞与架构边界违背。

### 结构化 Finding 与高信噪比降噪

每个 Finding 采用结构化字段：

- **严重程度（Severity）**：`Critical`（阻塞致命缺陷/安全风险）、`High`（功能逻辑错误/数据一致性破坏）、`Medium`（潜在性能问题/维护隐患）、`Low`（轻微建议/非阻塞优化）；
- **位置**：文件相对路径与精确行号范围；
- **分类（Category）**：`bug`、`security`、`performance`、`maintainability`、`test`；
- **问题与影响**：清晰阐述为何在真实场景下会造成实际损害，提供推理或复现证据；
- **建议修复方向**：提供可行动的修复指导或建议代码。

默认过滤 `Low` 级别噪音，仅对 `Critical` 和 `High` 设置门禁阻断，保证审查结果具备高信噪比与极高精准度。

## 简单任务：Adversarial Review

Reviewer 的心态：**尝试证明这段代码在真实场景下会失败。**

检查：

- 逻辑 Bug；
- 边界值；
- Null / Empty / Error Path；
- Regression；
- 错误假设；
- 缺失或无效测试；
- 测试是否真正覆盖修改行为。

## 中等任务：Spec Verifier + Cleaner

### Spec Verifier

逐条对照 Acceptance Criteria：

- 是否完整实现；
- 是否有只实现 Happy Path；
- API / Schema / 状态变化是否符合设计；
- 兼容行为是否正确；
- 测试是否能够证明要求成立。

### Cleaner

目标是减少复杂度，而不是增加抽象。

优先找：

- 重复；
- 死代码；
- 坏命名；
- 过深嵌套；
- 过长函数；
- 不必要状态；
- 无价值 wrapper；
- 为未来假想需求建立的抽象；
- 与仓库已有模式不一致的实现。

原则：`Prefer simplification over abstraction.`

## 复杂任务：增加 Architect Review

除上述两项外，再检查：

- 代码实现是否与已批准的 `docs/architecture/changes/YYYY-MM-DD-<task-slug>.html` 方案对比图拓扑一致，有无擅自偏离或扩充边界；
- 架构变更是否同步更新了 L1-L3 规范，且 `lint:arch` 门禁执行通过（0 错误、0 警告）；
- 模块边界是否正确；
- 依赖是否单向、是否产生循环依赖；
- 抽象层级是否合理；
- 数据和状态所有权是否清晰；
- 是否造成不必要的跨层耦合；
- 事务和一致性边界是否合理；
- 扩展点是否建立在真实需求上；
- 核心设计是否会让未来修改成本显著增加；
- 是否破坏现有架构原则。

## Documentation Gate

按照 [documentation-sync.md](documentation-sync.md) 验证文档影响结论。若代码改变模块边界、依赖关系、公共接口、核心流程、数据流、API、配置、部署拓扑或开发/构建/测试流程，而相关架构规范（L1-L3）、变更记录、Spec、Plan 或仓库级规则未同步更新，将其列为阻塞 Finding，Review 不得通过。

若任务涉及架构变更（`Architecture Impact: Required`），检查架构规范是否更新且 `lint:arch` 校验通过；若为复杂任务，进一步检查 `docs/architecture/changes/YYYY-MM-DD-<task-slug>.md` 语义记录是否与方案阶段的 HTML 对比图成对归档（已声明降级时检查回退图与原因引用）；未同步时 Review 不得通过。

新增职责与边界清晰的较大独立模块时，检查是否存在对应的 `docs/architecture/modules/<module-name>.md`、模块索引入口及必要架构图；缺失时 Review 不得通过。

`Docs Impact: None` 也必须能够由完整 Git Diff 支撑，不能作为跳过检查的默认结论。

## Finding 处理

主 Agent 对每条 Finding 做验证：

- Valid → 修复，并按需要补回归测试；
- Invalid → 不修改代码，仅记录判断依据；
- Uncertain → 通过代码、运行测试或最小复现验证。

有效 Finding 修复后重新运行相关测试；如果改动影响 Review 结论，应再次调用独立 Reviewer。

方案图格式遵循 [SKILL.md](../SKILL.md) 的分级规则：复杂任务且有架构变化时使用 `/archify` 生成 HTML 对比图并与变更语义记录成对归档（声明降级时引用回退图与原因）；复杂任务无架构变化及中等任务在 Plan 中嵌入简要 Mermaid。
