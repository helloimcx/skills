# Handler 规则与开发契约

本文档是 code-loop 中 **Handler 轮**（需求与缺陷处理实现）的唯一规则源（`rules_doc`）。

---

## 1. 目标与角色

Handler Main Agent 负责自主认领 GitHub Issue，在隔离的 Git worktree 中以 TDD 模式完成编码实现，推送到远端平台并创建 PR，同时维护项目 Issues 状态台账：
- **精准认领**：单轮只处理一项 Issue，绝不顺手超范围改造无关代码。
- **TDD 驱动**：先编写可复现问题的失败单测（RED），再编写实现让测试转绿（GREEN），最后重构（REFACTOR）。
- **反坍缩台账**：每次处理必须严格维护 `.agents/loops/state/handler-issues.md`，杜绝多 Issue 坍缩合并。

---

## 2. 标准开发生命周期

### §1 领取 Issue（Claim）
```bash
python scripts/control_plane.py claim --loop handler [--item issue:<NUMBER>]
```
- 若无待处理 Issue，输出简短总结结束。
- 成功返回分配的 `execution_id` 与 `fencing token`。

### §2 隔离环境准备（Worktree & Branch）
1. 基于最新的 `origin/<BASE_BRANCH>` 创建功能分支：
   ```bash
   git fetch origin
   git worktree add -b fix/<username>/issue-<NUMBER> <WORKTREE_PATH> origin/<BASE_BRANCH>
   ```
2. 向 CP 绑定该工作区：
   ```bash
   python scripts/control_plane.py bind --execution <EXECUTION_ID> --token <TOKEN> --worktree <WORKTREE_PATH> --branch fix/<username>/issue-<NUMBER>
   ```

### §3 TDD 编码实现
1. **编写针对性测试**：在 `tests/` 中编写新增或失败用例，运行 `pytest` 确认红灯。
2. **最小化代码实现**：编写业务逻辑，使测试变为绿灯。
3. **架构与文档检查**：若涉及公共接口或模块结构变动，同步更新文档。

### §4 门禁验证（Verify）
调用控制面运行全量项目验证考题：
```bash
python scripts/control_plane.py verify --execution <EXECUTION_ID> --token <TOKEN>
```
确保全量测试、代码风格和静态检查 100% 通过。

### §5 推送分支与创建 PR
1. 将提交推送到远端仓库：
   ```bash
   git push -u origin fix/<username>/issue-<NUMBER>
   ```
2. 创建 GitHub PR（使用 `gh pr create` 或 API）：
   - PR 标题明确包含 Issue 编号：`fix(core): resolve xyz (#<NUMBER>)`。
   - PR 正文包含关键字：`Fixes #<NUMBER>`，确保合并后 Issue 自动闭环。

### §6 维护台账与反坍缩校验（Ledger Update）
维护项目内部的 `.agents/loops/state/handler-issues.md`：
- **反坍缩硬红线**：快照表（`## 快照表`）中必须为**每个 Open Issue 单独保留一行**。
  - **严重违规模式**：`225/153/…/7 决策类 ×12` 这类把多个 Issue 压缩到单行的“批量行”，会导致冷存储漏消费。
  - **校验命令**：必须运行 `python scripts/check_ledger.py` 确保 exit 0，否则拒绝完成。

### §7 报告与发布（Report & Publish）
```bash
python scripts/control_plane.py report --execution <EXECUTION_ID> --token <TOKEN> --file report.json
python scripts/control_plane.py publish --execution <EXECUTION_ID> --token <TOKEN>
```

---

## 3. 非静默红线（Non-Silent Rule）

若在处理过程中发现 Issue 需求存在严重歧义、方案不可行或依赖外部输入而被迫置为 `BLOCKED`：
- **严禁静默放弃**：必须在 GitHub Issue 上发表清晰、详尽的说明评论，解释阻塞原因、尝试过的排查细节以及需要用户拍板的选择项。
- 随后将工作项标记为 `BLOCKED` 并更新台账。
