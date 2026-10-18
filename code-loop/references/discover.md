# Discover 规则与调度契约

本文档是 code-loop 中 **Discover 轮**（需求发现与任务盘点）的唯一规则源（`rules_doc`）。

---

## 1. 目标与角色

Discover Main Agent 负责定期盘点 GitHub 平台最新事实，完成需求过滤、任务分类与就绪队列入队：
- **全局唯一爬取路径**：依赖 `codeloop.cp.observer` 将远程 GitHub 平台的 Issues、PRs、评论与 Check Runs 增量抓取至本地 `observer.sqlite`。
- **任务分发**：根据项目状态推导需要消费的新工作项，将其由观察事实转化为 Control Plane 的 `READY` 工作项。

---

## 2. 标准发现生命周期

### §1 触发同步与抓取（Sync）
```bash
python scripts/control_plane.py sync
```
- 控制面调用 GitHub API 客户端拉取当前 base 分支最新 commit、所有 Open Issues 与 Open PRs。
- 比较本地已完成与在途执行，识别出未覆盖的新工作项。

### §2 任务分类与推导
1. **审查项（`review:<PR>`）**：
   - 所有状态为 `open` 且尚未完成合流的 PR，推导生成 `reviewer` 队列项。
   - 包含冲突状态标记（`has_conflicts`）、基础 CI checks 状态。
2. **处理项（`issue:<NUMBER>`）**：
   - 过滤已标记 `wontfix`、`duplicate` 或已关联进行中 PR 的 Issue。
   - 依据 Issue 标签与优先级将其排入 `handler` 队列。

### §3 状态输出与监控
```bash
python scripts/control_plane.py status
python scripts/control_plane.py list --all
```
Discover 轮无需创建分支或 worktree，完成增量扫描并确保队列刷新后即可结束。
