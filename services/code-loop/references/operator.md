# Operator 干预契约与升级阶梯

## 升级分级契约

当系统检测到异常、超时或阻塞时，Operator 依据分级阶梯执行自愈动作或升级：

1. **自动缓解（Mitigation）**：
   - `collect_refresh`: 立即刷新快照与项目状态。
   - `wal_checkpoint`: 执行 SQLite WAL checkpoint（TRUNCATE），清理积压日志。
   - `command_resend`: 重新分发未决命令。

2. **升级上报（Escalation）**：
   - 严重问题或多次自愈无效时，必须自动落 issue：
     - 调用 `github_create_issue`（或兼容别名 `gitea_create_issue`）在对应仓库中自动创建 Issue 跟踪。
     - 必要时调用 `github_comment_issue`（或兼容别名 `gitea_comment_issue`）追加现场诊断证据。
   - 若平台无法访问或需人工干预，执行 `escalate_human`。

## 动词支持状态
- `collect_refresh` (已接线)
- `command_resend` (已接线)
- `wal_checkpoint` (已接线)
- `github_create_issue` / `gitea_create_issue` (已接线)
- `github_comment_issue` / `gitea_comment_issue` (已接线)
- `escalate_human` (已接线)
