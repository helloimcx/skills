# Spec: Operator 到 Worker 常驻智能 Agent

## Goal

每台 worker 机器运行一个常驻 WorkerAgent 作为命令接收与执行监督入口。Operator 是 CP 侧的全局协调者，负责跨项目/机器消费事件、授权路由任务和追踪结果；WorkerAgent 负责常驻接入、task 校验与幂等；它为每条任务启动一次性的 worker-local Coordinator runtime。Coordinator 按任务分析本机证据或委派独立 Handler；Handler 实际修改 PR 仓库并遵循现有 CP 生命周期。runtime 本身不常驻。

## Scope

- 保留 Operator 对新出现的 GitHub `CHANGES_REQUESTED` review、code-loop Reviewer 已成功发布的 `Changes requested` 判词，以及 PR 新进入 `mergeable=false` 的检测和自动派发。
- 同一轮 CP sync 中同一 PR 的多种修复原因合并为一个事件和一个 `pr.repair` 任务；事件携带项目、PR、head/base 和原因身份，不携带 review 正文。
- 新增 machine-scoped 持久命令台账与版本化 typed task envelope，可指向机器、可选项目和具体资源。首期 task type 为 `pr.repair` 和 `worker.project_diagnose`；任务注册表可按同一 schema 扩展。
- `pr.repair` 由项目 WorkerAgent 准备现有 Handler claim、隔离 worktree 和 CP token，再启动本次 Coordinator runtime session；Coordinator 使用 runtime 原生能力启动独立 Handler subagent，继续沿用现有 verify/report/publish 护栏。
- `worker.project_diagnose` 支持排查指定机器/项目/loop 的停摆，例如 discover 长时间未运行。诊断证据包括 CP execution 与 worker 心跳镜像、本机 WorkerAgent/连接状态、项目注册配置、本机日志、watchdog 记录和已登记操作系统调度任务状态。证据缺失时报告未知项，不猜测根因。
- 允许 Coordinator 按任务授权自动修复和重启：仅能调用本地 allowlist 中的确定性维护动作，例如对已登记的 code-loop scheduler task/service 执行 start/restart，或在无活动 execution 时补跑一次 discover。所有动作均记录 command ID、动作参数、前后状态与验证结果。
- WorkerAgent 常驻接收命令；每个命令临时启动新的 Coordinator runtime 会话，需要时再启动独立 task subagent。默认 runtime/model 由 worker 本机配置，项目可以声明覆盖；Operator 不代远程机器选择本地 runtime。
- Operator 对新鲜 PR 修复事件和已有 worker stall 信号自动生成任务；支持离线排队、TTL、重连重放、状态回报和幂等。

## Non-goals

- 不让 runtime server 或某个 LLM 对话常驻；不跨任务复用会话上下文。
- 不把任意 shell 命令或无限制 prompt 作为通用 Operator 接口。任务类型、payload、runtime tool 权限和可自动执行的维护动作均采用 allowlist。
- 不自动处理启用前既有的 changes-requested/冲突 PR；不因单独的 `Validation failed`、普通评论、审批、draft/closed PR 或 `mergeable=null` 自动创建 PR 修复任务。
- 不直接改写外部调度器定义或未登记的系统服务；不自动 force-push、合并 PR 或绕过 CP 验证/发布流程。
- 不增加任意机器远程管理接口；所有任务仍由已注册、未吊销机器的 WorkerAgent 通过现有 TLS/Bearer 指令通道接收。

## Behavior / Interface

- CP sync 写入内部事件 `pr.handler_repair_needed`。原因包括 `changes_requested` 和 `merge_conflict`；外部 review 用 review ID 标识，内部判词用 SENT outbox operation ID 标识，冲突用 PR head/base 与状态边沿标识。首次 snapshot 不回补历史状态。
- 系统按 `worker_stall_seconds` 阈值检测机器/项目/loop 停摆。默认 discover 阈值沿用当前 18 小时；状态从未停摆进入停摆时写 `worker.loop_stalled` 事件，恢复时写 resolved 边沿。Operator 对一个未解决停摆只创建一个活动 `worker.project_diagnose` 任务。
- 同一 PR 同次 sync 有评审变化请求与冲突时只写一条事件，`repair_reasons` 同时保留两种原因。现有 `PR_REVIEW_FIX` / `PR_CONFLICT_RESOLUTION` work item 与 Handler claim 流程保持事实来源。
- Worker command envelope 有 schema version、稳定 command ID、task type、目标 machine、可选 project、结构化参数、权限/可执行维护动作、TTL 和来源事件 ID。命令不携带任意 shell argv；project-scoped task 必须通过 project grant 授权，machine target 必须是当前有效注册机器。
- CP command ledger 面向 machine 路由任务：命令可在 Worker 离线时保持 pending；机器重连后按授权和 TTL 补派。Worker hello 必须公告支持的 task schema；未公告兼容 schema 的旧 Worker 保持任务等待升级，不会收到新 typed frame。Operator task TTL 默认七天；过期任务只会在仍有项目 grant、本地注册仍有效且兼容 schema 的 worker 重连时重新排队，执行前必须重新验证任务对应的 PR head 或 stall 状态。重复入队使用稳定 source/event ID 幂等；同一目标的活动修复或诊断任务去重。
- WorkerAgent 根据 task type 的本地 allowlist handler 校验 envelope、项目范围、runtime/model 和权限，再启动一条新的 Coordinator runtime session。Runner capability 声明该 runtime 能否创建独立 subagent；不支持的 PR repair runner 在 claim 前拒绝。
- 对 `pr.repair`，WorkerAgent 用现有 CP claim/worktree/bind 链准备 execution，向 Coordinator 提供 command/execution ID、PR/head/base、task/reasons、worktree 和 token。Coordinator 只负责调用 `handler` subagent；Handler 负责改动、验证、报告与发布。命令帧不包含 review 正文。
- 对 `worker.project_diagnose`，WorkerAgent 先用确定性本机 adapter 收集带时间戳和来源的证据，再交给只读诊断能力解释；若任务授权允许维护，Coordinator 只能调用 envelope 允许且机器配置已登记的维护动作。动作执行后必须重新采样验证并返回报告。
- 诊断和维护报告至少包含：观察范围、证据来源与时间、发现、缺失证据、执行的维护动作、前后状态、结果及未解决项。报告写入 machine command ledger 并产生可供 Operator 消费的完成事件。
- command ID、execution ID、runtime session/child ID 以及维护动作 ID 可恢复关联。重复投递、ack 丢失或 WorkerAgent 重启时先对账状态；不重复启动活跃 Handler、不重复执行已成功的维护动作。失败应显式记录并按任务策略释放 claim/重试或转人工。
- 任意 maintenance action 必须执行本地确定性校验：精确匹配已登记服务/task、确认当前状态满足启动/重启条件、避免打断活跃 execution、记录前置检查、执行动作并验证结果。未知 task/service、参数越权或无法验证时拒绝执行并在报告中解释。
- CP project event 与 Worker task completion callback 都只负责唤醒 Operator；Operator 分别按项目 CP store 和 registry 中的持久化 event ID 顺序回放，并在成功 ingest 后推进对应 cursor。事务回滚产生的 wakeup 不会被当作事实处理。
- 诊断动作前再次确认同一 `last_execution_id` 仍停摆；恢复/新 execution 后的旧事件不能触发维护。Coordinator 只有在运行时输出包含原生 Task/subagent 调用和已完成结果时才算成功。
- PR repair 的 Worker task 只有在原生 Task/subagent 已完成且对应 CP execution 状态为 `SUCCEEDED` 后才报告成功；子会话结束但 CP claim 仍 active、missing 或 failed 时必须带 execution/token fence 报失败。
- PR 持续处于 merge conflict 时，新的 PR head 或 base SHA 也构成新 repair 输入；此前 head 已过期的终结任务不会阻止新版本任务入队。

## Constraints / Compatibility

- 复用 CP 持久事件和机器注册/项目 grant；新增 machine command ledger、命令状态/结果 API 与 worker ack/progress protocol。兼容旧 WorkerAgent：不支持新 task schema 的 worker 不接收该版本任务，命令保留为明确失败/等待升级状态。
- Operator 保留既有冷却、日额、事件白名单、项目资源 lease 与稳定游标；PR 自动修复事件首次消费游标不回补旧事件。worker stall 告警使用可配置阈值与边沿去重。
- runtime 由执行机器按自身安装和凭据验证；machine default 可以有 per-project override。不要依赖 Operator 主机上的 runtime/model 清单。
- task type 与 maintenance action 独立 allowlist。LLM 输出只能选择已授权 action ID，不能生成新命令、扩展文件范围或提升任务权限。
- PR 修复与维护状态都持久化；所有可变更操作必须可从 command ID 审计与重放。
- 本功能 PR 以 `main` 为 base。仓库已通过 PR #24 将 `master` 历史合并进 `main` 并将 `main` 设为默认分支；本功能分支同时包含 PR #22 的 Operator consumer 提交，避免重复实现该改动。

## Acceptance Criteria

1. 新观察到的外部 `CHANGES_REQUESTED` review、当前 head 上已 SENT 的 `Changes requested` 判词和 PR 新进入 `mergeable=false` 各自生成稳定 PR 修复事件；重复 sync 不重复生成。
2. 初次 snapshot 中已有的变化请求/冲突不回补；Approval、单独 `Validation failed`、普通评论、draft/closed PR、错误 base、`mergeable=null` 不触发自动修复。
3. 同一 PR 同次 sync 同时出现 review request 和 conflict 时只生成一条带两种原因的事件、一个活动 `pr.repair` 命令和一个 Handler child。
4. 超过配置阈值的 discover stall 产生一次 `worker.loop_stalled` 边沿事件；恢复后产生 resolved 事件；同一未解决停摆不会重复派发诊断任务。
5. worker command 仅能投递给有效注册机器；项目目标必须通过该机器的项目 grant；未知 task type、坏 schema、过期命令和越权项目在 WorkerAgent 执行前被明确拒绝。
6. PR 修复任务在执行 worker 上启动新的 Coordinator session 和独立 Handler child，Handler 只在已准备 worktree 内工作并遵守现有 claim/verify/report/`edit_pr`/publish 契约；Coordinator 不直接编辑 worktree。
7. discover 诊断报告读取 CP 最近 execution/心跳、本机 WorkerAgent 与 watchdog 日志、项目注册和已登记 scheduler 状态；每项结论带来源/时间，缺少调度器证据时明确报告未知。
8. 获得 `allowlisted_remediation` 权限的诊断任务可对已登记任务执行允许的 start/restart，或在无活动 execution 时补跑一次 discover；每项动作有审计记录和 post-check。未知 task、非 allowlisted 命令、活跃 execution 冲突和校验失败都不执行。
9. 命令重复投递、离线重连、ack 丢失和 WorkerAgent 重启不会重复启动活跃 child 或重放已完成的维护动作；失败、成功和证据报告可从 CP machine command ledger 查询。
10. Dashboard 现有手动 review/repair 入口及旧 `review_round`/`repair_round` 命令保持可用。
