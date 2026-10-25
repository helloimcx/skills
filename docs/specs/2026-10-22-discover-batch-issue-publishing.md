# Discover 批量创建 GitHub Issue 规格

## Goal

让一次 code-loop Discover execution 可以经 Control Plane 创建多个有证据、去重后的 GitHub Issues，并在发布中断后逐条恢复，不重复创建已成功的 Issue。

## Scope

- 增加 Discover 专用的 `actions` report 字段，单个 report 携带多个 `create_issue` 动作和 `{title, body}` 条目。
- 保留既有 `create_issue` 动作和 report 格式，确保现有 worker 向后兼容。
- 每条新 Issue 具有独立 outbox operation ID、幂等标记和发布回执。
- 批次索引贯穿远程 worker 的 `publish/begin` / `publish/commit` 往返。
- 批量部分成功时已成功条目保持 SENT；重试只对账/完成未决条目。
- 更新 code-loop Discover 规则和当前 Skills Discover 定时任务，要求检查代码、证据、去重、批量创建并确认 handler 入队。
- 每轮可以创建多个独立 Issue；只报告证据充分且不被已有开放 Issue/PR 覆盖的问题。

## Non-goals

- 不改变 Handler/Reviewer 的领取、验证或发布流程。
- 不直接由 Discover 修改代码、推送分支或创建 PR。
- 不绕过 CP outbox 直接调用 GitHub 写 API。
- 不新增 SQLite 表或迁移既有 outbox 数据。

## Behavior / Interface

Report additive action:

```json
{
  "decision": "CONFIRMED",
  "reviewer": "Discover Main Agent",
  "summary": "发现两个独立且有证据的问题",
  "actions": [
    {"action": "create_issue", "payload": {"title": "Issue A", "body": "Evidence and expected behavior"}},
    {"action": "create_issue", "payload": {"title": "Issue B", "body": "Independent evidence and impact"}}
  ]
}
```

- `actions` 必须包含 2–10 个条目；仅 Discover 可以使用，且每项必须是 `create_issue`，payload 严格只有非空 `title` 与字符串 `body`。
- 每项仍通过既有 GitHub `create_issue` API 发布；CP 为每项建立独立 outbox 记录和唯一 marker。
- execution 只有在批次中所有 outbox 项都为 SENT 后才终结为 SUCCEEDED。
- 第一条 outbox intent 落盘后冻结 report；不允许以相同 execution 替换批次内容。
- `publish` 返回逐项回执；部分失败时保持未决，重试先按 marker 查询远端事实，不盲目重新 POST。
- 原有 `create_issue` report 不变。

## Constraints / Compatibility

- 批量 `actions` 仅允许 Discover role 且只允许 `create_issue`；handler/reviewer 权限不变。
- 仍遵循 `CONFIRMED` 决策、报告字段校验和 CP fencing token。
- 不改变既有 `outbox` schema；单 Issue action 保持既有 operation ID 和恢复路径。
- GitHub 限流/断网可能令某条 operation outcome unknown；未知结果必须经 marker 对账，不能自动重复发送。

## Acceptance Criteria

1. Discover 可用一个 report 请求多个独立 Issue；handler/reviewer 使用该动作会被拒绝。
2. 非列表、空列表、空标题、额外字段或错误 item 类型在产生任何 outbox 写入前被拒绝。
3. 多条 Issue 均成功时，每条都有独立远端编号和 marker，execution 仅在全部 SENT 后完成。
4. 批次中途成功后发生不确定错误，再次 publish 不重复创建已成功 Issue，并逐项报告可确认回执。
5. 既有 `create_issue` 单项 report、publish 和回执查询语义保持不变。
6. Discover 文档和定时任务明确允许每轮多个高置信度、未覆盖 Issue，并继续遵守 CP 发布路径。
7. 单条批次 Issue 已发布后，不同 report 不能复用同一 child operation ID 或旧回执。

## Architecture Impact

Required. 这是 CP 的增量发布协议扩展，涉及 report validation、outbox intent、发布恢复和终态推导；数据表不迁移，但状态机需要识别批次的逐项完成事实。
