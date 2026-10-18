# Control Plane 协议与运行时契约

本文档详述 code-loop Control Plane (CP) 协调器的状态机模型、验证考题契约、统一动词面与远程接入架构。

---

## 1. 架构原则

1. **确定性状态单点**：所有工作项的状态转换（租约锁、围栏令牌、outbox 事务）均由协调器推导，采用 SQLite `BEGIN IMMEDIATE` 保证事务串行化，无网络脑裂风险。
2. **免心跳租约（Heartbeat-free Leases）**：以租约截止时间（Lease Expiry）替代长轮询心跳。执行机掉线或崩溃后，租约过期由协调器自动回收并重新放回就绪队列，无需维持活性心跳。
3. **围栏令牌（Fencing Token）**：每次领取（`claim`）单调自增生成全新 token。任何操作（`bind` / `verify` / `report` / `publish`）必须校验 token 一致性；迟到写或残留进程的请求一律被 `STALE_INPUT` 或 `TOKEN_MISMATCH` 阻断。
4. **单动词面（Uniform Verb Surface）**：无论协调器运行在本机进程内还是局域网远端 CP Server 上，CLI 动词面完全一致。CLI 通过 `--config` 判定人格：带 `--config` 为单机内联协调者；省略 `--config` 自动切为执行机 Worker 人格。

---

## 2. 状态空间与流转

每个工作项（Work Item）及对应执行（Execution）遵循严格的状态机流转：

```
                ┌─────────┐
                │  READY  │
                └────┬────┘
                     │ claim
                     ▼
                ┌─────────┐
                │ RUNNING │
                └────┬────┘
                     │ bind + verify
                     ▼
              ┌─────────────┐
              │  VERIFYING  │
              └──────┬──────┘
                     │ report + publish
        ┌────────────┼────────────┐
        ▼            ▼            ▼
  ┌───────────┐ ┌─────────┐ ┌──────────┐
  │ SUCCEEDED │ │ BLOCKED │ │  FAILED  │
  └───────────┘ └─────────┘ └──────────┘
```

- **READY**：工作项已在平台上就绪（如新 Open 的 Issue 或 PR），等待 Worker 领取。
- **RUNNING**：Worker 已领取租约并建立执行上下文，处于分支检出与代码编辑阶段。
- **VERIFYING**：代码编辑完成，工作区已绑定，进入门禁测试验证阶段。
- **SUCCEEDED**：验证 100% 通过，report 提交且 publish 成功推送到远端平台。
- **BLOCKED**：遇到业务决策阻塞、合并冲突或权限不足，必须附带说明留在平台后挂起。
- **FAILED**：验证测试失败、代码损坏或无法修复的不可逆错误。
- **TIMED_OUT**：租约到期仍未完成，协调器自动回收状态。

---

## 3. 考题契约（`.code-loop/verify.json`）

自 2026-09-17 起，项目的验证标准与门禁规则由**项目仓库自身**的 `.code-loop/verify.json` 唯一指定：

```json
{
  "verification": [
    ["{python}", "-m", "pytest", "tests/", "-q"]
  ],
  "required_checks": [
    "ci/build",
    "lint"
  ]
}
```

### 核心安全规则

1. **Base Commit 考题锁定**：CP 仅根据 sync 观测到的 **base 分支 commit** 读取考题文件。PR 变更分支不能改动其自身运行的考题，从根源杜绝 PR 自行降低门禁要求的作弊行为。
2. **考题变更流程**：修改验证命令必须作为针对 base 分支的独立 PR 合并后，下一轮方生效。
3. **考题篡改护栏（`exam.touched_by_pr`）**：当 PR diff 精确触碰 `.code-loop/verify.json` 时，发布阶段会自动追加强制安全警示行并打上审计事件。
4. **Fail-Closed 故障回退**：若文件损坏或读取格式非法，立即拒绝 claim（报 `EXAM_INVALID` / `EXAM_UNAVAILABLE`），绝不静默放行；仅在文件缺失（404）时平滑回退到机器本地旧配置。
5. **Checks 与 Status 双引擎覆盖**：`required_checks` 名单同时覆盖 GitHub Actions Check Runs（以 Job/Workflow 名匹配，要求 `status == 'completed'` 且 `conclusion == 'success'`）与 Commit Status API（以 context 名匹配，要求 `state == 'success'`）。

---

## 4. 统一 CLI 动词面

所有操作由 `control_plane.py` 提供统一入口：

```bash
# 1. 同步平台事实（GitHub 增量拉取）
python scripts/control_plane.py sync

# 2. 状态查询与连通性自检
python scripts/control_plane.py status
python scripts/control_plane.py doctor

# 3. 列出队列中工作项
python scripts/control_plane.py list [--kind {discover,handler,reviewer}] [--all]

# 4. 领取工作项（Claim）
python scripts/control_plane.py claim --loop reviewer [--item review:<PR>] [--worker <label>]
# 返回 JSON: {"id": "...", "token": 1, "item": {...}}

# 5. 绑定工作区（Bind）
python scripts/control_plane.py bind --execution <ID> --token <TOKEN> --worktree <PATH> [--branch <BRANCH>]

# 6. 执行门禁验证（Verify）
python scripts/control_plane.py verify --execution <ID> --token <TOKEN>

# 7. 提交总结与判词（Report）
python scripts/control_plane.py report --execution <ID> --token <TOKEN> --file <report.json>

# 8. 发布到平台（Publish：发评论/合并PR/推分支）
python scripts/control_plane.py publish --execution <ID> --token <TOKEN>

# 9. 启停控制（Operator 运维）
python scripts/control_plane.py enable
python scripts/control_plane.py disable
```

---

## 5. 远程模式（Remote Access）

局域网或 VPN 内的辅助机器可作为 Worker 节点接入中央协调机（CP Server）：

1. **服务端（主开发机）**：
   - 运行 `python scripts/cp_server.py serve`（单进程双端口：8722 协调面 + 127.0.0.1:8712 面板 UI）。
   - 服务端单点管理 SQLite 状态与锁，Worker 只执行本地 worktree 检出、测试验证与 Git push。
2. **Worker 注册（Pairing / Enroll）**：
   - Worker 机执行配对请求：
     ```bash
     python scripts/control_plane.py enroll --url https://cp.lan:8722 --name dev-box
     ```
   - 服务端管理员审核并批准：
     ```bash
     python scripts/cp_server.py joins approve --name dev-box
     ```
3. **项目授权（Project Grant）**：
   - Worker 提出项目授权申请：
     ```bash
     python scripts/control_plane.py request-project --project org__repo
     ```
   - 服务端批准授权：
     ```bash
     python scripts/cp_server.py requests approve --id <ID>
     ```
4. **自检（Doctor）**：
   - 在 Worker 机执行 `python scripts/control_plane.py doctor`，校验 TLS 钉扎指纹、Token 鉴权、解释器与工作区根目录配置均已就绪。
