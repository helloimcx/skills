# Operator 干预契约与升级阶梯

本文档定义 code-loop 的中心 Operator 与 worker 本机 Coordinator 的职责、查证路径、动作边界与升级分级契约。

## 角色定位与职责边界

Operator 是 CP 侧的全局运维协调者，跨项目和 worker 消费持久事件、确定任务目标、授权路由并跟踪结果。它不在 worker 上运行项目 runtime。每台 worker 上的常驻 `WorkerAgent` 是受认证的任务接收入口和执行监督者；它校验 task schema、机器/项目授权与本地注册，记录幂等状态，并按任务启动一次性的 Coordinator runtime。

- **Coordinator**：worker 本机的按任务智能协调者，每个 machine task 使用新的 runtime 会话。PR 修复时它只负责启动独立 Handler；worker 诊断时它分析本机采集的证据并选择任务授权范围内的 action ID。Coordinator 不能直接修改项目文件或执行任意 shell。
- **Handler**：实际处理 PR 仓库改动的 agent，使用现有 CP claim、worktree、verify、report 和 publish 生命周期。
- **本机 adapter**：WorkerAgent 对 Coordinator 选择的 allowlisted action ID 执行确定性校验、固定系统调用和 post-check，是机器动作的最终执行者。

具体流程如下：
- **事件派发**：CP 新观察到 changes-requested review、merge conflict 或 discover 停摆后，Operator 将 typed task 投递给获授权且已注册项目的兼容 worker。Worker hello 会公告 task schema 版本；旧 worker 不接收不支持的 typed task。首次启用不回补历史 PR 事件；任务默认保留七天，离线任务重连后会重新检查 PR head 或停摆状态。持续冲突下 PR head/base 更新时也会生成新的 repair 事件。
- **本机协调**：Coordinator runtime/model 从 worker 本机或项目注册读取。PR repair 必须通过 runtime 原生 subagent/Task 能力拉起独立 Handler；WorkerAgent 还会确认对应 CP execution 已完成 `verify/report/publish` 并进入 `SUCCEEDED`，之后才把 task 标为成功。
- **worker 诊断**：`worker.project_diagnose` 汇集 CP 最近 execution、WorkerAgent 连接状态、项目注册、本机 watchdog/worker 日志和已登记 scheduler 状态。缺失证据必须标为未知。
- **受限自愈**：诊断 Coordinator 只能返回获授权的 action ID；worker 每次动作前再核对同一停摆仍对应相同的最后 execution、本机注册、CP 活动 execution 和目标状态，之后才调用固定 adapter。重复 action ID 在一次结果中只执行一次；任意 shell 和模型提供的 target/argv 都不会执行。
- **中心侧升级**：本地 Operator 仍负责告警记录、事件审计和中心侧升级动作；worker 诊断/修复结果保存在 machine task ledger，并由 Operator 消费完成事件。

## Worker Coordinator 配置

Worker runtime 安装与凭据必须在项目机器本地可用。可在机器全局 `~/.code-loop/control-plane.json` 设置默认值，也可通过 `dashboard.py register --project <org__repo> --coordinator-agent opencode --coordinator-model <provider/model>` 为单项目覆盖 agent/model。Coordinator 目前支持 Claude Code 和 OpenCode；不具备原生 subagent 能力的 runtime 会在 claim 前拒绝 PR repair。

worker 全局配置可登记唯一允许维护的 Windows scheduler/service target：

```json
{
  "coordinator_agent": "opencode",
  "coordinator_model": "<provider/model>",
  "maintenance": {
    "scheduler_tasks": {
      "discover": {"name": "CodeLoop-Discover", "path": "\\"}
    },
    "services": {
      "worker_agent": "CodeLoopWorkerAgent"
    }
  }
}
```

只登记机器上真实存在的精确 task/service 名称。诊断任务的 `allowed_actions` 还必须包含该 action；未登记、非 Windows 平台、CP 仍有活动 execution 或 post-check 未通过时，worker 会跳过动作并把原因写入结果。`discover.run_once` 通过项目已登记 runtime 启动标准 discover prompt，并且只在 CP 确认没有活动 discover execution 后执行。

## 升级分级契约

当系统检测到异常、超时或阻塞时，Operator 依据分级阶梯执行自愈动作或升级：

1. **自动缓解（Mitigation）**：
   - `collect_refresh`: 立即向服务端申请刷新项目快照与状态空间，拉齐最新状态。
   - `wal_checkpoint`: 执行 SQLite WAL checkpoint（TRUNCATE 模式），强制刷盘并清理积压日志，解决并发读写瓶颈。
   - `command_resend`: 重新分发超时或未确认的未决命令，恢复执行机实时响应。

2. **升级上报（Escalation）**：
   - 严重问题或多次自愈无效时，必须自动落 issue：
     - 调用 `github_create_issue`（或兼容别名 `gitea_create_issue`）在对应仓库中自动创建 Issue 跟踪。
     - 必要时调用 `github_comment_issue`（或兼容别名 `gitea_comment_issue`）追加现场诊断证据。
   - 若平台无法访问或需人工干预，执行 `escalate_human`。

## 动词支持状态与目标参数格式

| 动词 | 类别 | 状态 | 目标参数 `--target` 规范格式 | 说明 |
|---|---|---|---|---|
| `collect_refresh` | Mitigation | 已接线 | 空或省略 | 触发看板后台刷新数据采集 |
| `command_resend` | Mitigation | 已接线 | `<review_round\|repair_round>:<number>:<org__repo>` | 重新下发指定未决轮次命令 |
| `wal_checkpoint` | Mitigation | 已接线 | `<db path>`（省略则默认仪表主库） | 针对指定库执行 SQLite WAL checkpoint (TRUNCATE) |
| `github_create_issue` / `gitea_create_issue` | Escalation | 已接线 | `<org__repo>:<title>`（或 `<org/repo>:<title>`） | `--detail <body_text>` 创建跟踪 Issue |
| `github_comment_issue` / `gitea_comment_issue` | Escalation | 已接线 | `<org__repo>:<issue#>`（或 `<org/repo>:<issue#>`） | `--detail <comment_body>` 追加调查记录 |
| `escalate_human` | Escalation | 已接线 | `<org__repo>:<title>`（或 `<org/repo>:<title>`） | `--detail <body_text>` 自动加 `[❓needs-human]` 标记人工介入 |
| `no_op` | Local | 本地执行 | 空或省略 | 状态正常，无需采取干预措施 |
| `sweep_expired` | Local | 本地执行 | 空或省略 | 清理超期未关闭的死锁与僵尸记录 |

## 命令行交互契约

在 Operator 轮中，Main Agent 的工作目录为 state 目录，查证与动作统一经由稳定 CLI 执行：

```bash
# 1. 状态查证（读取 live rounds、ready signals、近期 commands）
python scripts/operator.py status

# 2. 动词执行（需携带本轮分配的 execution 与 token）
python scripts/operator.py act --execution <E> --token <T> --verb <verb> [--target <target>] [--detail <detail>]

# 3. 提交本轮总结报告
python scripts/operator.py report --execution <E> --token <T> --file report.json
```
