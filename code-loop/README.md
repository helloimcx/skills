# code-loop Skill

本地确定性自动化研发闭环 Agent Skill。

为 AI 编程 Agent 提供与 `code-loop` 服务的确定性对接规则、统一动词面 CLI、三大研发循环（`discover` / `handler` / `reviewer`）规范、L1 自治运维契约，以及门禁与反坍缩台账防护。

## 主要特性

- **纯标准库零安装**：纯 Python 3.9+ 与本地 SQLite，无需 Docker 或中间件；
- **确定性状态协调**：Control Plane 统一推导状态，免心跳租约超时回收，围栏令牌防脑裂；
- **仓库级考题锁定**：以项目自身 `.code-loop/verify.json` 为门禁基准，Base Commit 锁定读取，严禁 PR 擅自降级门禁；
- **端到端审查与合并**：Reviewer 隔离 worktree 门禁测试验证、覆盖率把关、PR 冲突自愈并自动调度合并；
- **TDD 开发与反坍缩台账**：Handler 规范化开发闭环，快照表逐 Issue 追踪，`check_ledger.py` 自动化检测；
- **L1 运维自治（Operator）**：基于白名单执行 WAL 清理、采集刷新与未决重发，多次无效自动向 GitHub 提交 Issue 上报；
- **单进程双端口网关**：`cp_server.py serve` 同步提供 8722 局域网协调 API 与 `127.0.0.1:8712` 纯静态仪表看板；
- **多机协同（Remote Mesh）**：支持开发机/GPU 机作为执行 Worker 接入主控端，经由 TLS 指纹与 Token 鉴权。

## 目录结构

```text
code-loop/
├── SKILL.md                  # Skill 核心入口与 Agent 指引
├── README.md                 # 本文
├── references/               # 深度规则与契约规范
│   ├── control-plane.md      # CP 协调协议、状态机与考题契约
│   ├── reviewer.md           # 审查与合并门禁规范
│   ├── handler.md            # 需求处理与反坍缩台账规范
│   ├── discover.md           # 事实盘点与调度规范
│   └── operator.md           # L1 自治分级干预与升级阶梯
└── scripts/                  # 路径稳定的薄 CLI 委托入口
    ├── _shim.py              # fail-closed 服务定位器
    ├── control_plane.py      # CP 协调器主接口
    ├── cp_server.py          # 网关服务管理 CLI
    ├── dashboard.py          # 面板采集与项目登记 CLI
    ├── worker_agent.py       # 常驻执行机 Agent
    ├── trigger.py            # 事件门铃触发层
    ├── operator.py           # 运维轮次 CLI
    ├── check_ledger.py       # 台账反坍缩校验器
    └── test_shims.py         # Shim 门禁单测
```

## 安装与使用

### Via skills.sh CLI (`npx skills`)

```bash
npx skills add helloimcx/skills --skill code-loop
```

### 手动安装

将本目录直接拷贝至 Agent 的 Skill 目录：
- Claude Code: `~/.claude/skills/code-loop/`
- Codex: `~/.codex/skills/code-loop/`
- Antigravity: `~/.gemini/config/skills/code-loop/`

> **说明**：当单独拷贝技能目录到其他环境时，由于脱离了仓库目录结构，必须通过以下方式之一让 Shim 定位到 `services/code-loop` 服务（否则会触发 `SERVICE_NOT_FOUND` fail-closed）：
> 1. 执行一次钉扎：`python <repo>/services/code-loop/scripts/register_home.py`（写入 `~/.code-loop/service.home`）
> 2. 或配置环境变量：`export CODE_LOOP_SERVICE=<path/to/services/code-loop>`

