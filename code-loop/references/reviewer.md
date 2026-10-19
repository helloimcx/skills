# Reviewer 规则与评审契约

本文档是 code-loop 中 **Reviewer 轮**（自动化审查与合并门禁）的唯一规则源（`rules_doc`）。

---

## 1. 目标与角色

Reviewer Main Agent 负责对 GitHub Pull Request 进行端到端确定性审查、门禁验证与合并调度：
- **客观验证**：以项目仓库自身 `.code-loop/verify.json` 考题为唯一准绳，严禁以主观感受替代测试验证。
- **环境隔离**：每次评审必须检出独立 Git worktree，严禁污染宿主主工作区。
- **原子发布**：评审结论（LGTM / BLOCKED / FAILED）由 CP 统一推导并向 GitHub 原子发布。

---

## 2. 标准审查生命周期

```
┌─────────┐      ┌───────────────┐      ┌─────────────────┐
│ §1 领取 │ ───► │ §2 准备与绑定 │ ───► │ §3 差异与覆盖率 │
└─────────┘      └───────────────┘      └─────────────────┘
                                                 │
                                                 ▼
┌─────────┐      ┌───────────────┐      ┌─────────────────┐
│ §6 发布 │ ◄─── │ §5 提交报告   │ ◄─── │ §4 门禁验证     │
└─────────┘      └───────────────┘      └─────────────────┘
```

### §1 领取工作项（Claim）
```bash
python scripts/control_plane.py claim --loop reviewer [--item review:<PR_NUMBER>]
```
- 若返回 `null` 或空结果，表示当前无待审 PR，直接输出总结结束。
- 成功返回 `{"id": "<EXECUTION_ID>", "token": <TOKEN>, "item": {"key": "review:<PR>", ...}}`。

### §2 准备与绑定（Bind）
1. 在配置的 `worktree_root` 下为当前 PR 检出独立 worktree：
   ```bash
   git fetch origin pull/<PR_NUMBER>/head:pr/<PR_NUMBER>
   git worktree add <WORKTREE_PATH> pr/<PR_NUMBER>
   ```
2. 调用 CP 绑定：
   ```bash
   python scripts/control_plane.py bind --execution <EXECUTION_ID> --token <TOKEN> --worktree <WORKTREE_PATH>
   ```

### §3 差异与代码覆盖率检查
1. 提取变更文件清单与 diff：
   ```bash
   git diff origin/<BASE_BRANCH>...HEAD
   ```
2. 规则审查：
   - 是否包含未经允许的外部依赖引入？
   - 是否触碰架构红线或敏感文件？
   - 若触碰 `.code-loop/verify.json`，需核查变更是否符合考题安全护栏（严禁弱化验证）。
3. 覆盖率门禁：新增或修改的核心业务逻辑必须配有对应单元测试。

### §4 门禁验证（Verify）
在绑定的 worktree 内调用控制面门禁验证：
```bash
python scripts/control_plane.py verify --execution <EXECUTION_ID> --token <TOKEN>
```
- CP 会自动代入 base 分支中的 `.code-loop/verify.json`，执行验证命令阵列。
- 若任一命令非 0 退出，状态机记为失败，终止放行。

### §5 判词裁决与提交报告（Report）
构造标准的评审报告 JSON 文件（`report.json`）：
```json
{
  "verdict": "LGTM",
  "summary": "全量单元测试通过，架构规范与覆盖率达标，准予自动合并。",
  "checks": {
    "verify_exam": "PASSED",
    "required_checks": "PASSED",
    "architecture": "PASSED"
  },
  "feedback": "无阻塞性问题。"
}
```
判词枚举：
- `LGTM`: 验证全部通过，无阻塞性问题，满足合并条件。
- `BLOCKED`: 存在需作者确认的业务分歧、依赖阻塞或需修复的冲突。
- `FAILED`: 单元测试失败、静态语法错误或考题被非法修改。

提交报告：
```bash
python scripts/control_plane.py report --execution <EXECUTION_ID> --token <TOKEN> --file report.json
```

### §6 发布与清理（Publish & Cleanup）
```bash
python scripts/control_plane.py publish --execution <EXECUTION_ID> --token <TOKEN>
```
- CP 自动向 GitHub PR 提交正式评审评论，若为 LGTM 且 CI 全部绿灯，自动触发合并动作。
- 清理本次评审创建的临时 worktree：
  ```bash
  git worktree remove <WORKTREE_PATH> --force
  ```

---

## 3. 定向评审（看板手动轮）

当用户从监控看板点击某张 PR 卡片时，触发定向评审：
1. Agent 提示词附带定向后缀：`本轮为看板手动定向评审：目标只有 PR #{number}`。
2. 若提示词指出看板已完成准备与绑定（`PREPARED` 态），**严禁重复 claim / bind**，直接从 §3 检查与 §4 验证切入。
3. 严格遵循单个 PR 边界：只评审指定的一个 PR，发布后立即退出，绝不继续抢领后续 PR。

---

## 4. PR 冲突修复流（Conflict Repair）

当 PR 与主分支存在代码冲突时：
1. CP 识别到 `has_conflicts: true`，将工作项标记为待冲突修复。
2. 修复 Agent 检出 PR 分支，执行 `git merge origin/<BASE_BRANCH>` 或 `git rebase`。
3. 本地解决冲突后，重新运行全量单测，通过后 push 到 PR 分支并通知 CP。

---

## 5. 红线

1. **考题不可越权**：严禁在评审过程中修改 `.code-loop/verify.json` 以求通过。
2. **非静默红线**：只要给出非 `LGTM` 的结论，必须在 report 中留存详尽根因分析与代码定位，CP 会确保平台可见。
3. **工作区整洁**：评审结束后无论成功失败，必须完整移除分配的 temporary worktree，防止磁盘泄漏。
