# Discover 规则与调度契约

本文档是 code-loop 中 **Discover 轮**（问题发现、Issue 创建与任务盘点）的唯一规则源（`rules_doc`）。

---

## 1. 目标与权限

Discover Main Agent 定期检查仓库中的代码问题，并结合 GitHub 当前事实完成去重、Issue 发布与队列刷新。

- **读取事实**：通过 `codeloop.cp.observer` 同步 GitHub base commit、开放 Issues/PRs、评论与 Check Runs；不要绕过 observer 直接抓取同一批数据。
- **检查仓库**：只读检查代码、配置、文档、近期变更和相关验证结果；Discover 不实现修复、不创建分支或 worktree、不领取 handler/reviewer 项。
- **允许的写入**：只创建、评论或按 CP 规则关闭 GitHub Issue；创建 Issue 必须经 Control Plane 的 `report` / `publish`，不直接调用 GitHub API，也不使用普通 shell 或 `gh issue create` 绕过门禁。
- **批次上限**：每轮可以创建多个新 Issue；Control Plane 单批最多接受 10 条。优先报告影响明确、可复现、尚未被已有 Issue/PR 覆盖的问题，不为凑数量而创建。

## 2. 标准发现生命周期

### §1 同步 GitHub 事实

```bash
python scripts/control_plane.py sync
python scripts/control_plane.py status
python scripts/control_plane.py list --all
```

检查同步错误和最近的开放 Issues/PRs。若 `sync` 失败或事实未知/过期，停止 Issue 发布，记录错误；不得把同步故障当成“仓库没有问题”。

### §2 领取 Discover 工作项并检查仓库

```bash
python scripts/control_plane.py claim --loop discover
```

本轮只领取一个 discover 工作项。基于仓库内容进行有范围的只读检查，优先查看近期变更、明显失败路径、未被验证覆盖的关键行为，以及此前未跟踪的具体异常。不要为满足每轮创建数量而制造任务。

Issue 候选必须有仓库证据，例如具体文件/符号/行、稳定复现步骤、失败命令或明确违反的既有契约。单纯的重构偏好、没有证据的猜测、已修复或已由开放 Issue/PR 覆盖的问题不创建。

### §3 去重并决定是否创建

对候选逐一与已同步的开放 Issues/PRs 核对标题、描述、受影响路径和根因。已有工作覆盖同一问题时不重复创建。保留所有相互独立且证据充分的候选，按影响和优先级排序；超过 10 条时只提交前 10 条，并在总结中列明剩余候选未纳入本轮。若没有证据充分的候选，记录缺少的证据并结束，不创建 Issue。

没有合格候选时，通过本轮 discover execution `report` 记录 `REJECTED`（检查完成且无新问题）或 `NEEDS_MORE_EVIDENCE`（存在候选但证据不足），不带发布动作，随后简短总结。

### §4 经 Control Plane 发布 Issue

创建前先领取的 execution 必须仍有效。多个候选放在同一份 report 的 `actions` 数组中，每条动作独立创建一项 Issue：

```json
{
  "decision": "CONFIRMED",
  "reviewer": "Discover Main Agent",
  "summary": "一句话说明问题、证据和影响",
  "actions": [
    {
      "action": "create_issue",
      "payload": {
        "title": "清楚描述一个可执行问题",
        "body": "问题表现、影响范围、复现步骤或验证证据、期望行为、相关文件/符号。"
      }
    },
    {
      "action": "create_issue",
      "payload": {
        "title": "另一个独立问题",
        "body": "该问题自己的表现、影响和可核实证据。"
      }
    }
  ]
}
```

```bash
python scripts/control_plane.py report --execution <ID> --token <TOKEN> --file <report.json>
python scripts/control_plane.py publish --execution <ID> --token <TOKEN>
```

`actions` 必须包含 2–10 条；每条只能使用 `create_issue`，payload 只能包含 `title` 与 `body`。CP 会为每条添加独立幂等标记并逐项通过 outbox 发布；批次部分成功时，重试会对账已发项目并恢复未决项。若某项结果未知且找不到唯一回执，停止自动重试并如实报告，禁止盲目重发。

### §5 确认入队并汇报

```bash
python scripts/control_plane.py sync
python scripts/control_plane.py status
python scripts/control_plane.py list --all
```

确认所有新 Issue 编号及其 handler 队列状态；同步失败时如实报告，不声称已入队。最终总结包括：检查范围、创建的各 Issue 编号和证据，或未创建的具体原因，以及同步/队列错误。

## 3. 禁止事项

- 不因“本轮应该有产出”而虚构漏洞、需求或影响。
- 不创建重复、过宽、不可行动或没有可核实证据的 Issue。
- 不在 Discover 轮领取 handler/reviewer 工作、修改代码、实现修复、推送分支或创建 PR。
- 不直接调用平台写 API；所有 Issue 创建、评论、关闭都须通过 CP 的 report/publish 并遵守其门禁。
