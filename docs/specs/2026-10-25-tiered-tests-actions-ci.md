# 测试分层与 GitHub Actions CI

状态：实现与独立 Review 完成；本地 QA 通过，等待 GitHub Actions 托管运行。

## Goal

开发时获得快速反馈，PR 阶段自动运行完整的仓库测试，统一 code-loop 文档与既有验证分层行为。保留 Reviewer 的完整验证和 base commit 考题护栏。

## 当前证据

- 基线：`origin/main`，`a155b93`；2026-10-25 查询 GitHub，默认分支为 main，Actions 工作流数量为 0。
- main 传统分支保护开启管理员约束，但 `required_status_checks` 为 null；仓库 rulesets 为空。CI 成功与强制阻止合并是两项不同能力。
- `resolve_verification(cfg, kind)` 已实现 Reviewer 使用 `verification`，其他轮次使用非空 `verification_fast`，未配置时回退 `verification`。
- Handler 文档仍要求推送前全量验证；SKILL 与 control-plane 的考题示例未说明快速层。
- 当前没有仓库 `.code-loop/verify.json`；不得假设机器本地考题内容，或承诺仅修改文档就会加速现有部署。
- Python 3.9.6 本地执行根目录 unittest：17 项通过，报告耗时 1.237 秒；其中包含 project-setup 分发契约、prototype-first 检查器及 code-loop shim 子测试。
- AST 盘点服务测试：71 个 `test_*.py`，1,331 个测试方法；这是静态盘点，不是本次全量通过证据。

## Scope

1. 更新 `code-loop/SKILL.md`、`references/handler.md`、`references/control-plane.md`、`references/reviewer.md` 中的测试与交付说明。
2. 增加 `.github/workflows/ci.yml`，在 PR、main push 和手动触发时执行完整 CI。
3. 更新根 README、CLAUDE、服务 README 及现有架构文档，说明本地与 CI 的验证入口、覆盖范围和检查名称。
4. 增加必要的工作流契约检查，验证触发条件、测试入口完整性和最终结果汇总，避免核心套件遗漏或失败后误绿。

## Non-goals

- 本次不修改 CP 运行时代码、考题解析、安全护栏、发布状态机或凭据处理。
- 不引入按 diff 自动选择测试的框架，也不缓存或复用 Reviewer 验证证据；Reviewer 仍完整验证。
- 不创建生产部署、镜像构建、定时任务、操作系统兼容性矩阵或外部服务测试。
- 不修改远端分支保护、审查人数、管理员约束、绕过设置或 rulesets。文档清楚说明：需要强制阻止合并时，须在真实 CI 跑通后单独配置稳定的 `CI Gate` 检查。
- 不在本次添加仓库 `.code-loop/verify.json` 或修改机器本地配置。目标项目启用快速层须由该项目维护者另行提交考题配置变更，遵守独立 PR 与 base commit 生效规则。

## Behavior / Interface

### 本地与自动化轮次

| 时机 | 必需行为 |
|---|---|
| 编码及修复迭代 | 运行修改点、相关模块及边界回归测试；风险高或影响不明时扩大范围 |
| Handler 推送前 | 先完成相关测试，再调用原有 `verify`；配置快速层则使用快速层，否则完整验证 |
| GitHub PR / main push | 自动完整执行仓库级及服务测试，不按文件路径跳过工作流 |
| Reviewer 合并前 | 继续执行完整 `verification`，并检查项目配置的 required checks；CI 不自动替代本地证据 |

文档示例同时展示 `verification`、`verification_fast` 和 `required_checks`，示例命令必须标明目标项目与工作目录，不把 skills 仓库专用路径复制为通用项目命令。快速验证只是提交前反馈层，不能声明为完整回归。

### GitHub Actions

- Workflow 名为 `CI`；监听 `pull_request`（默认活动）、`push` 到 main、`workflow_dispatch`。PR 测试默认合并引用，main push 测试实际 main 提交。
- Ubuntu hosted runner，使用 Python 3.9 验证现有最低兼容声明；Ubuntu 验证不等于 Windows/macOS 兼容验证。
- 两个独立 job：仓库契约与服务套件，并行执行；单一解释器避免为每个 PR 重复运行完整套件。
- 仓库契约：`python -m unittest discover -s tests -p 'test_*.py'`。根测试已执行分发契约、设计检查器和 shim 子套件，不额外重复运行这些子套件。
- 服务套件：`python -m unittest discover -s services/code-loop/tests -p 'test_*.py'`，运行时区固定为 `Asia/Shanghai` 以验证产品声明的本地日历日期，不把 pytest 专有 conftest 行为宣称为 unittest 保证。
- 仓库 job 增加 Python 源文件编译检查，范围使用显式的仓库代码目录；不编译 `.git` 或 worktree 元数据。
- 固定最终 job/check 名 `CI Gate`；`needs` 所有测试 job，使用 `always()`，只有全部依赖结果严格为 success 才成功，失败、取消或意外跳过均不得误绿。
- 工作流无顶层 paths 过滤、无文档 PR 例外、无 `continue-on-error`；PR 更新按 PR/ref 分组取消过时 CI，main 事件单独分组。
- 最小权限 `contents: read`，checkout 禁止持久化凭据；官方 Actions 固定经过核实的完整 commit SHA 并注明版本；明确 job 超时。
- 测试配置本地 git author 身份以支持临时仓库提交；不访问真实账号、生产数据库、用户服务或生产凭据。
- 使用已有标准库 unittest 测试入口，无需新增运行时依赖。工作流 YAML 的解析/静态校验若使用额外工具，单列为开发验证依赖，不改变服务零第三方运行时依赖声明。

## Constraints / Compatibility

- 保持 `resolve_verification` 的既有兼容行为，未配置快速层的项目继续完整验证。
- CI 初期保留 Reviewer 与 CI 的部分重复验证，避免把本次扩大为可信证据复用设计。
- 保持 skill 自包含；skill 文档不要求安装者读取本仓库根文档才能工作。
- 本地验证与托管 Actions 执行分别记录；未发生的远端执行不得标记通过。

## Acceptance Criteria

1. Handler、Reviewer、考题示例对快速层、完整层、回退条件的说明一致，原有分层单测及 Worker 端到端测试通过。
2. PR、main push、手动运行三个入口均在 workflow 中声明；普通分支 push 不重复触发另一轮 CI。
3. CI 覆盖根 unittest 和完整服务 unittest，包含两个 Python 版本；没有遗漏根测试调用的 skill 子套件。
4. 任一矩阵 job 失败、取消或跳过时，`CI Gate` 不成功；全部成功时汇总成功。
5. 工作流使用只读权限、固定 Action SHA、凭据不持久化、明确超时和隔离并发组，静态校验通过。
6. 开发、验证与架构文档同步，明确 CI 默认运行不等于已经设为合并必需检查。
7. 独立 Reviewer 检查最终完整 diff；本地执行约定入口并记录测试数量、耗时及真实失败，不静默跳过。

## Architecture Impact

Architecture Impact: Required。新增共享验证执行平台与反馈流程；CP 运行时职责、公共 API、数据模型及安全边界不变。沿用现有 Markdown 架构载体，更新 `docs/architecture.md`、`docs/architecture/modules/code-loop.md` 并添加语义变更记录；仓库尚无 L1-L3 JSON 规范或 lint:arch 入口，本次不另造格式或引入工具链。

## References

- [GitHub Python 测试与固定 Action SHA 建议](https://docs.github.com/en/actions/tutorials/build-and-test-code/python)
- [GitHub 必需状态检查及跳过工作流问题](https://github.com/github/docs/blob/main/content/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks.md)
- [GitHub 工作流事件及 PR 引用](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
