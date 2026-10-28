---
name: code-loop
description: 本地确定性自动化研发闭环（Control Plane 协调器、GitHub 审查与合并调度、门禁修复、Operator 监控自治与 Dashboard）
---

# code-loop — 本地确定性自动化研发闭环

`code-loop` 是一个纯 Python 3.9+ 标准库与本地 SQLite 构建的机器本地确定性研发闭环系统。它通过 Control Plane 协调器统一调度 GitHub Issues 的自主实现、Pull Requests 的自动化审查与合并门禁、系统异常的自治自愈，以及可视化实时看板。

---

## 1. 核心架构与设计哲学

```
┌────────────────────────────────────────────────────────────────────────┐
│                              GitHub 平台                               │
│              (Issues / Pull Requests / Actions CI / Checks)            │
└───────────────────────────────────▲────────────────────────────────────┘
                                    │ REST API (GitHub.py)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        code-loop 服务单元                              │
│                                                                        │
│   ┌──────────────────────────┐         ┌───────────────────────────┐   │
│   │   Control Plane 协调器   │         │     Dashboard 监控看板    │   │
│   │   (cp/ · 状态机与租约)   │         │    (dash/ · 纯静态UI)     │   │
│   └─────────────┬────────────┘         └─────────────▲─────────────┘   │
│                 │                                    │                 │
│                 ▼                                    │                 │
│   ┌──────────────────────────────────────────────────┴─────────────┐   │
│   │           cp_server.py serve (单进程双端口合并服务)            │   │
│   │     • 8722: 局域网协调 API       • 127.0.0.1:8712: 面板 UI     │   │
│   └──────────────────────────────────┬─────────────────────────────┘   │
│                                      │                                 │
│                 ┌────────────────────┴───────────────────┐             │
│                 ▼                                        ▼             │
│   ┌──────────────────────────┐             ┌───────────────────────┐   │
│   │  worker_agent.py (常驻)  │             │   operator.py (自治)  │   │
│   │  (WebSocket RFC6455通道) │             │   (L1 故障分级治理)   │   │
│   └──────────────────────────┘             └───────────────────────┘   │
└────────────────────────────────────────────────────────────────────────┘
```

1. **纯标准库与零外部依赖**：仅需 Python 3.9+ 与本地 SQLite，无需 Docker、Redis 或大型中间件，安装即用。
2. **免心跳租约与围栏令牌**：状态流转单点化，租约超时自动回收，围栏令牌（fencing token）防止跨进程或迟到操作脑裂。
3. **考题唯一源（`.code-loop/verify.json`）**：验证门禁标准存放在目标项目根目录下，且**锁定读取 base commit**，杜绝 PR 分支自行降低门禁标尺。
4. **三大研发循环**：
   - `discover`：定时盘点平台事实，识别新 Issues / PRs 并入队。
   - `handler`：认领 Issue，在独立 worktree 中以 TDD 实现；先跑相关测试，再由项目 `verification_fast` 考题提供快速门禁（缺省时完整验证）；提 PR 并维护反坍缩台账。
   - `reviewer`：认领 PR，执行隔离门禁验证与覆盖率检查，提交 LGTM / BLOCKED / FAILED 判词并调度合并。
5. **Operator 自治与分级升级**：基于 `operator.md` 白名单执行 WAL 检查点、状态采集刷新与未决重发；严重问题自动向 GitHub 提交 Issue 升级。

---

## 2. 常用 CLI 工具清单

Skill 的 `scripts/` 目录提供路径稳定的委托 CLI 入口（自动路由至底层服务单元）：

| 脚本 | 核心功能 | 典型命令 |
|---|---|---|
| `scripts/control_plane.py` | CP 协调器主接口（统一动词面） | `python scripts/control_plane.py status`<br>`python scripts/control_plane.py claim --loop reviewer`<br>`python scripts/control_plane.py doctor` |
| `scripts/cp_server.py` | 合并服务网关（8722 协调 + 8712 UI） | `python scripts/cp_server.py serve`<br>`python scripts/cp_server.py init` |
| `scripts/dashboard.py` | 面板采集与项目登记 | `python scripts/dashboard.py collect`<br>`python scripts/dashboard.py register --project org__repo` |
| `scripts/worker_agent.py` | 常驻执行机 Agent（指令通道） | `python scripts/worker_agent.py` |
| `scripts/trigger.py` | 事件门铃触发层 | `python scripts/trigger.py --config <triggers.json> run` |
| `scripts/operator.py` | L1 自治运维轮次 CLI | `python scripts/operator.py status`<br>`python scripts/operator.py act --verb collect_refresh` |
| `scripts/check_ledger.py` | Handler 台账反坍缩校验器 | `python scripts/check_ledger.py` |

---

## 3. Agent 运行指引

当作为 `code-loop` 闭环中的某个 Worker 轮执行时，请严格根据所属循环遵循对应规范：

### A. Reviewer 轮（审查与合并门禁）
> 详见 [references/reviewer.md](references/reviewer.md)

1. **领取（Claim）**：
   ```bash
   python scripts/control_plane.py claim --loop reviewer [--item review:<PR>]
   ```
   若返回 `null`，输出总结后正常退出。
2. **隔离绑定（Bind）**：在独立 worktree 检出 PR 代码并绑定：
   ```bash
   python scripts/control_plane.py bind --execution <ID> --token <TOKEN> --worktree <PATH>
   ```
3. **门禁验证（Verify）**：
   ```bash
   python scripts/control_plane.py verify --execution <ID> --token <TOKEN>
   ```
4. **判词报告与发布（Report & Publish）**：
   ```bash
   python scripts/control_plane.py report --execution <ID> --token <TOKEN> --file report.json
   python scripts/control_plane.py publish --execution <ID> --token <TOKEN>
   ```
   发布后清理工作区。

### B. Handler 轮（需求与缺陷处理）
> 详见 [references/handler.md](references/handler.md)

1. **领取 Issue**：
   ```bash
   python scripts/control_plane.py claim --loop handler
   ```
2. **分支与 Worktree**：基于主分支创建 `fix/...` 或 `feature/...` 分支并在新 worktree 中绑定。
3. **TDD 实现与验证**：编写测试（RED）→ 编写实现（GREEN）→ 门禁校验：
   ```bash
   python scripts/control_plane.py verify --execution <ID> --token <TOKEN>
   ```
4. **推送与 PR**：推送远端分支并创建 Pull Request（正文含 `Fixes #<ISSUE>`）。
5. **维护反坍缩台账**：更新 `.agents/loops/state/handler-issues.md`，执行 `python scripts/check_ledger.py` 确保每一条 Open Issue 独立占有一行，严禁合并为批量行。
6. **报告与发布**：调用 `report` 与 `publish`。

### C. Discover 轮（问题发现、Issue 创建与就绪编排）
> 详见 [references/discover.md](references/discover.md)

1. **同步既有事实**：
   ```bash
   python scripts/control_plane.py sync
   ```
2. **检查并核对问题**：检查仓库代码和近期变更，结合同步到的开放 Issues/PRs，找出有明确证据、尚未被已有工作覆盖的问题；不得把猜测或纯偏好包装成缺陷。
3. **创建 Issue 或记录结论**：每轮可创建多个优先级较高、证据充分且去重后的 GitHub Issues（CP 每批上限 10 条）。先 `claim --loop discover`，再按 `references/discover.md` 使用 CP `report` / `publish` 发布；无合适问题时报告结论，不创建 Issue。
4. **检查队列**：确认新 Issue 已同步为 handler 队列项，并汇报 Issue 编号或未创建的客观原因。Discover 只检查和发布 Issue，不修改仓库代码，也不领取 handler/reviewer 项。

### D. Operator 轮（监控巡检与自愈升级）
> 详见 [references/operator.md](references/operator.md)

1. **状态查证**：
   ```bash
   python scripts/operator.py status
   ```
2. **分级自愈**：
   - 缓解：`collect_refresh` / `wal_checkpoint` / `command_resend`。
   - 升级：多次自愈无效时，调用 `github_create_issue` / `github_comment_issue` 在仓库建 Issue 归档现场证据。

---

## 4. 平台集成与配置（GitHub）

### 1. 凭据配置
服务优先读取环境变量 `GITHUB_TOKEN`，或机器本地凭据文件 `~/.code-loop/credentials/GITHUB_TOKEN`，并支持本地 `gh` CLI 自动继承身份（`gh auth token`）。

### 2. 目标仓库考题配置（`.code-loop/verify.json`）
在受控项目根目录添加门禁定义。下面是 Python / pytest 项目的示例；按目标项目的真实测试入口调整。Handler 轮可选使用快速考题，Reviewer 轮始终运行完整 `verification`：
```json
{
  "verification": [
    ["{python}", "-m", "pytest", "tests/", "-q"]
  ],
  "verification_fast": [
    ["{python}", "-m", "pytest", "tests/test_issue_module.py", "-q"]
  ],
  "required_checks": [
    "CI Gate"
  ]
}
```

`verification_fast` 可省略或设为空列表；此时所有轮次均回退到完整验证。配置新的 required check 前，先在目标仓库运行工作流并确认 GitHub 上的实际检查名。`.code-loop/verify.json` 的更改须通过独立 PR 合并到 base 后才生效。

### 3. 项目配置文件（`~/.code-loop/projects/<org__repo>/control-plane.json`）
```json
{
  "base": "main",
  "platform": {
    "url": "https://api.github.com",
    "repo": "myorg/myrepo"
  },
  "worktree_root": "/path/to/worktrees",
  "state_dir": "/path/to/state",
  "remote": false
}
```

---

## 5. 红线与防御纪律

1. **考题防篡改**：严禁在 Reviewer 轮中弱化或修改 `.code-loop/verify.json` 规则。
2. **反坍缩台账**：Handler 维护的 Issues 台账快照表必须**每个 Open Issue 单独一行**，`check_ledger.py` 报错立即返工。
3. **非静默阻塞**：任何标记为 `BLOCKED` 的操作，必须在 GitHub 平台对应 Issue/PR 留下详尽客观分析与证据评论。
4. **Worktree 隔离与回收**：所有实现和审查操作均在独立 Git worktree 内进行，任务完成即刻彻底清理。

---

## 6. 深入参考文档

- [Control Plane 协议规范与运行时契约](references/control-plane.md)
- [Reviewer 审查规则与合并门禁契约](references/reviewer.md)
- [Handler 需求实现与反坍缩台账契约](references/handler.md)
- [Discover 事实盘点与调度契约](references/discover.md)
- [Operator 故障分级干预与自愈阶梯契约](references/operator.md)
