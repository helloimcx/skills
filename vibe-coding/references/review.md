# Review 细则

Review 目标是找出作者会愿意修复的具体问题，而不是证明 Reviewer 聪明。

## Reviewer 通用规则

- 使用一个独立 Subagent；
- Reviewer 不参与本轮实现；
- Review 时只读，不修改代码；
- 读取最终 Spec / Acceptance Criteria；
- 读取 Architecture & Documentation Impact Analysis；
- 同时检查 Code Diff、Architecture Diff、Documentation Diff，以及必要的上下文、调用方和测试；
- 继续检查完整改动，不因找到第一个问题而停止；
- 只报告具体、可验证、可行动的问题；
- 区分阻塞问题和建议，不把个人风格偏好升级成阻塞问题。

每个 Finding 最好包括：

- 严重程度；
- 文件和位置；
- 问题是什么；
- 为什么会造成真实影响；
- 建议修复方向。

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

若任务涉及架构变更（`Architecture Impact: Required`），检查 `docs/architecture/changes/YYYY-MM-DD-<task-slug>.md` 语义记录是否与方案阶段的 HTML 对比图成对归档，且 `lint:arch` 校验通过；未同步时 Review 不得通过。

新增职责与边界清晰的较大独立模块时，检查是否存在对应的 `docs/architecture/modules/<module-name>.md`、模块索引入口及必要架构图；缺失时 Review 不得通过。

`Docs Impact: None` 也必须能够由完整 Git Diff 支撑，不能作为跳过检查的默认结论。

## Finding 处理

主 Agent 对每条 Finding 做验证：

- Valid → 修复，并按需要补回归测试；
- Invalid → 不修改代码，仅记录判断依据；
- Uncertain → 通过代码、运行测试或最小复现验证。

有效 Finding 修复后重新运行相关测试；如果改动影响 Review 结论，应再次调用独立 Reviewer。
