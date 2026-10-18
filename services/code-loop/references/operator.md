# Operator 干预契约与升级阶梯

本文档定义 code-loop 系统中 Operator 轮（自治运维与监控治理）的查证路径、动作边界与升级分级契约。

## 角色与职责

Operator 是 code-loop 的 L1 机器运维协调 Agent：
- **查证（Verification）**：直接读取机器本地 Operator 存储库（`~/.code-loop/operator.sqlite`），监控未决告警、运行中轮次（live rounds）与看门狗重启记录，严禁盲信传入参数。
- **自治自愈（Self-Healing）**：在白名单边界内执行确定性自愈动作。
- **分级上报（Escalation）**：多次自愈无效或发现系统性红线时，向代码托管平台（GitHub）创建 Issue 并追加现场诊断证据。

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
