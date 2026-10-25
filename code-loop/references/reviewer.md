# Reviewer 规则与评审契约

本文档是 code-loop 中 **Reviewer 轮**（PR 独立审查与门禁判定）的唯一规则源（`rules_doc`）。Reviewer 提供证据和判定；CP 校验后发布正式评审评论。批准只表示符合手工合并条件，不授权 Agent 自动合并。

---

## 1. 判定与权限边界

Reviewer 报告的 `decision` 必须使用 CP 接受的以下值：

- `Approved for manual merge`：本轮验证通过、覆盖度完整、所需 CI 检查通过，可由有权限的人手工合并。
- `Changes requested`：发现可修复的问题；报告摘要和评审评论应指出文件、位置及所需更改。
- `Blocked`：当前无法完成有效判断，例如外部依赖或环境问题。必须提供与本轮验证证据相符的 `gate` 说明；不能用它代替代码修改请求。
- `Validation failed`：门禁命令失败或验证无法通过。报告应写明失败命令和实际输出摘要。

Reviewer 不得合并 PR、推送主分支或将批准表述为自动合并授权。CP 的批准发布是给人的评审结果；合并仍由获授权的人执行。

---

## 2. 标准审查生命周期

1. 使用 `python scripts/control_plane.py claim --loop reviewer [--item review:<PR_NUMBER>]` 领取单个 PR。无结果时结束本轮。
2. 按 CP 配置准备独立 worktree，并用 `bind --execution <ID> --token <TOKEN> --worktree <PATH>` 绑定。
3. 检查 PR diff、架构约束、测试覆盖和 `.code-loop/verify.json`。不得降低或修改门禁来取得通过。
4. 运行 `python scripts/control_plane.py verify --execution <ID> --token <TOKEN>`。报告必须对应当前 PR head 和本轮验证结果。
5. 对 diff 中每个文件记录 `coverage`：完整审查的文件列入 `reviewed`；跳过的文件列入 `skipped` 并说明原因。批准必须提供完整且 `ok: true` 的 coverage；不得虚构已通过的测试、CI 或覆盖度。
6. 提交符合本文件 schema 的 `report.json`，随后调用 `publish`。首次评审（或 PR head 已变化）必须请求 `review_comment`，以便正式判定确实发布到 PR。
7. 发布后回收本轮 worktree。

CP 还会验证独立 Reviewer 身份、当前 PR head、required checks 和报告时序。批准需要该 PR 无冲突、required checks 全部成功及本轮 verification 通过。所需远程检查尚未成功时不得报告批准。

---

## 3. 报告格式

当前 CP 的 report 字段白名单是 `decision`、`reviewer`、`summary`、`action`、`payload`、`coverage`、`gate` 和 Discover 专用的 `actions`。Reviewer 必填 `decision`、`reviewer`、`summary`。Reviewer 报告不接受旧字段 `verdict`、`checks`、`feedback`，也不接受 Discover 批量字段 `actions`。

常规评审请求用单个 `action` 和 `payload`。首次发布正式 PR 评审时，`action` 为 `review_comment`，`payload.number` 必须是被评审 PR 编号，`payload.body` 写明具体发现或说明。CP 会附上判定、head/base SHA、coverage 摘要和其身份标记。已在同一 head 发布过判定评论时，重跑确认可省略 action。

批准报告示例的 coverage 值仅说明结构。实际提交前必须以真实 diff 逐项填充文件和计算值；不得复制示例当作验证证据：

```json
{
  "decision": "Approved for manual merge",
  "reviewer": "reviewer-agent",
  "summary": "本轮验证通过；diff 文件均已检查，required checks 成功。",
  "action": "review_comment",
  "payload": {
    "number": 123,
    "body": "检查结论与需要作者关注的说明。"
  },
  "coverage": {
    "total": 1,
    "reviewed": ["src/example.py"],
    "skipped": [],
    "ok": true,
    "rate": 1.0
  }
}
```

失败报告示例只记录失败，不声明通过：

```json
{
  "decision": "Validation failed",
  "reviewer": "reviewer-agent",
  "summary": "`python -m pytest` 退出码为 1；失败用例为 test_example。",
  "action": "review_comment",
  "payload": {
    "number": 123,
    "body": "验证失败：请查看本轮报告中的失败命令和诊断摘要。"
  }
}
```

将 `reviewer-agent`、PR 编号、文件清单和验证描述替换为本轮真实信息。`coverage` 的键和数据必须满足 CP 校验：`total` 是文件数；`reviewed` 与 `skipped` 不重叠且数量合计等于 `total`；每个 skipped 项必须包含非空 `path` 和 `reason`；`rate` 必须等于 reviewed 数量除以 total（空 diff 为 1.0）；`ok` 只可在所有 diff 文件均有审查或合理跳过记录时为 true。`Approved for manual merge` 必须包含 coverage 且 `ok: true`。

`Blocked` 报告需要与本轮验证 evidence 一致的 `gate`，由 CP 对照验证记录校验。非批准判定可以省略 coverage；若提供，也必须符合相同结构约束。非批准判定应在 summary 或评审评论中给出可复核的根因和代码位置。

提交并发布：

```bash
python scripts/control_plane.py report --execution <EXECUTION_ID> --token <TOKEN> --file report.json
python scripts/control_plane.py publish --execution <EXECUTION_ID> --token <TOKEN>
```

---

## 4. 定向评审与冲突

看板已完成准备与绑定（`PREPARED` 态）时，不得重复 claim 或 bind；从 diff 审查和 CP verify 开始。每轮仅处理指定 PR。

PR 与 base 冲突时，不得批准。将冲突细节和受影响文件写入评审结果并请求作者解决；冲突修复由 PR 作者或 Handler 单独完成。Reviewer 不修改 PR 分支。

---

## 5. 红线

1. 不得更改 `.code-loop/verify.json` 来放宽审查门禁。
2. 不得伪造 coverage、required checks、测试或独立审查证据。
3. 不得把 `Approved for manual merge` 描述成自动合并；未获授权不得合并或向主分支推送。
4. 评审结束后移除本轮创建的 worktree。
