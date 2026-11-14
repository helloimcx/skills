# handler 处置台账

## 快照表（2026-11-06，全量 Open Issues）

| # | 标题 | 状态 | 理由 |
|---|------|------|------|
| 41 | [❓needs-human] loop_stall 默认阈值 7.5h 与每日自动化节奏不匹配 — handler/discover 每日误报并空耗 Operator 轮次 | OPEN | 等待后续 Handler 轮 |
| 39 | [❓needs-human] collect_stale 每 75 分钟周期性复现 — 缺少周期采集调度，Operator 只能反复 collect_refresh | OPEN | 等待后续 Handler 轮 |
| 30 | [Operator] 报告无法终态化，actions 校验回归导致 Operator 轮永久 RUNNING | OPEN | 等待后续 Handler 轮 |
| 29 | [code-loop][Operator] discover 定时循环因 Codex 用量额度超限失败，两个项目 discover 已停滞约 42 小时 | OPEN | 等待后续 Handler 轮 |
| 27 | [vibe-coding] 按项目实际能力约束架构文档同步与 lint:arch 门禁 | OPEN | 等待后续 Handler 轮 |
| 21 | [Operator] 增加模型可用性、调用预算与显式备选模型策略 | OPEN | 等待后续 Handler 轮 |
| 20 | [Operator] 增加隔离演练模式，验证异常发现、诊断与拟执行动作 | OPEN | 等待后续 Handler 轮 |
| 19 | [Operator] 增加同类故障聚合、Issue 复用与历史处置经验查询 | OPEN | 等待后续 Handler 轮 |
| 18 | [Operator] 增加事故时间线与回放视图，串联信号、诊断、动作和恢复 | OPEN | 等待后续 Handler 轮 |
| 17 | [Operator] 增加“为什么没动”解释器，定位 Issue、PR 与 Worker 阻塞原因 | OPEN | 等待后续 Handler 轮 |
| 16 | [Operator] 在 Dashboard 展示启用状态、运行健康和信号跳过原因 | OPEN | 等待后续 Handler 轮 |
| 15 | [Operator] 强化报告证据校验，区分确认正常、已修复和证据不足 | OPEN | 等待后续 Handler 轮 |
| 14 | [Operator] 为自愈动作增加恢复验证与“观察中”后续复查 | OPEN | 等待后续 Handler 轮 |
| 13 | [Operator] 增加统一信号诊断接口，提供新鲜度明确的结构化现场 | OPEN | 等待后续 Handler 轮 |
| 12 | [Operator] 修复等待队列事件被取走后未处理，导致自动巡检信号丢失 | OPEN | 等待后续 Handler 轮 |

## 轮次日志

- 2026-11-03 / 处理 repair:32（PR #32，关联 Issue #27）：合并最新 main 并解决台账 add/add 冲突；根据 GitHub 当前 OPEN 列表刷新快照。

- 2026-10-26 / 处理 Issue #26：更新 Reviewer 与运行时契约并增加回归测试；PR 发布后刷新台账状态。
- 2026-10-28 / 处理 repair:34（PR #34，关联 Issue #26）：补充批准与验证失败报告示例的 CP schema/action 回归覆盖；等待 Handler 修复发布。
- 2026-10-31 / 处理 repair:37（PR #37，关联 Issue #25）：合并最新 main 并解决台账 add/add 冲突，保留每个 Open Issue 的独立行。
- 2026-11-01 / 处理 repair:38（PR #38）：响应 reviewer 对 Clash / Tailscale 文档与修复脚本的变更请求；PR #38 等待发布结果。
- 2026-11-02 / 处理 repair:38（PR #38）：保留 TLS 校验并将固定站点探测改为非判定性示例，显示 HTTP 状态和传输错误。
