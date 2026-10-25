# code-loop service

code-loop 的机器本地确定性运行时（服务单元）：Control Plane 协调器（`cp/`）、监控面板（`dash/`）、事件触发层（`trigger.py`）、**合并服务**（`cp_server.py serve`：单进程双端口——8722 协调面 + `127.0.0.1:8712` 面板 UI）、**机器任务通道**（`cp/ws.py`、`cp/worker_tasks.py`、常驻 `agent.py`、typed task executor 与本机 runtime runner）与其测试。对应薄 skill 见 `code-loop/`（规则文档 + 路径稳定的委托 CLI：control_plane/dashboard/trigger/cp_server/worker_agent/operator/check_ledger）；集成背景与契约见 `docs/specs/2026-10-18-code-loop-service-and-skill-integration.md`。

## 边界

- 纯 Python 3.9+ 标准库 + 本地 SQLite。`cp_server.py serve` 是**用户手动拉起的 opt-in 局域网协调网关，同时承载面板 UI**（dashboard 已并入其 loopback listener：一个进程两个端口，UI 仍只绑 127.0.0.1）——不是系统服务；进程由**看门狗**（`watchdog.py`，本服务一部分）巡检自愈：按命令行定位 `cp_server.py` / `worker_agent.py` 真实进程，缺失即 `schtasks /run` 拉起并验证新进程，cp-server 另验 8712 HTTP 探活，每轮留痕 `~/.code-loop/logs/watchdog.jsonl`。建议注册为 schtasks 周期任务（每 5 分钟，`--check` 为只读巡检）；`dashboard.py serve` 退役为指向合并命令的提示入口（`collect`/`register` 子命令不变）。职责上，CP 侧 **Operator** 负责全局事件消费、授权路由和结果追踪；每台执行机的常驻 **WorkerAgent**（`worker_agent.py`）负责认证接入、task 校验、幂等和执行监督；每条机器任务临时启动本机 **Coordinator** runtime，需要时由它委派独立 Handler，runtime 不常驻。worker 掉线时 task 留在机器台账，重连后补派；cron 轮继续作为项目状态的正确性地板。
- 依赖方向单向：skill → service。本服务不 import `skills/` 任何模块；规则文档、验证命令等均为机器本地配置注入的路径。
- 机器本地状态全部在 `~/.code-loop/`（config、SQLite、锁、钉扎证书、凭据、dashboard.sqlite、`server/`），不入任何仓库。
- 平台事实只有一条爬取路径（`cp/observer.py` + `~/.code-loop/observer.sqlite`）：CP server 的 driver 线程按 `observer_period`（默认 120s）刷新，CP 与 dashboard 都读同一份；CP/collector 仅在 store 缺失或过旧时内联爬取兜底（fail-open，语义不变）。
- **远程接入（可选）**：局域网/VPN 内其他开发机可作为 discover/handler/reviewer worker 接入。协调状态单点在服务端（SQLite/BEGIN IMMEDIATE/fencing/租约/outbox 语义零改动），worker 本地执行 worktree 校验、验证命令与 git push，经 TLS（ca 钉扎或 TOFU 指纹）+ 每机器 Bearer token 通信。统一管理（注册/授权/启停/吊销）只走服务端本机 `cp_server.py`，不开网络管理端点。契约与部署见 `skills/code-loop/references/control-plane.md` 远程模式一节。

## 面板 SQLite 连接与 CPU 排查

面板在启动时通过 `dash.schema.connect()` 初始化数据库。生产 HTTP 查询、POST 的项目/Issue 校验和后台快照刷新，每次通过 `connect_readonly()` 打开自己拥有的只读连接，在同一线程内完成查询、取完结果后关闭；采集和回复仍使用各自的写连接。只读连接不重复建表或切换 WAL。

`serve.create(db, ..., db_factory=...)` 接受连接工厂；兼容的 `create(db, ...)` 调用由处理器内部锁保护完整查询，但调用方也必须避免在处理器外同时操作该连接。`check_same_thread=False` 只关闭 Python 的线程检查，不保证 SQLite 连接可以被并发使用。尤其 `THREADSAFE=2` 编译模式要求一个连接及其游标同一时刻只由一个线程使用（[SQLite 官方契约](https://sqlite.org/threadsafe.html)）。

若服务持续占用多个 CPU 核心，先保留进程采样。2026-10-23 的故障中，约 600% CPU 对应 6 个线程停留在 `sqlite3_reset → sqlite3_db_release_memory → purgeableCacheShrink`；代码中存在无锁共用面板连接的缺陷。修复消除了这一危险访问模式，原生缓存内部异常的完整触发过程仍未确定。已经卡住的旧线程需要在应用修复后重启服务才能退出。

## 考题文件 `.code-loop/verify.json`（2026-09-17 起，考题唯一源）

review 验证标准放在**各项目仓库**的这个文件里（仅两键；机器配置同名键降级为迁移回退）：

```json
{
  "verification": [["{python}", "-m", "pytest", "tests/", "-q"]],
  "required_checks": ["ci/build"]
}
```

- `verification` 是 argv 二维数组（`{python}`/`{worktree}` 占位）；`required_checks` 是放行前的 CI context 名单（首尾空白规范化）。未知键拒绝。
- CP 按 sync 观测到的 base commit 读取——PR 分支改不了自己的考题；**改验证标准 = 对 base 分支的普通 PR**，合并后下一轮生效（在途轮 `STALE_INPUT` 重领新版）。仅 404（文件不存在）回退机器配置两键；坏文件或读取失败 fail-closed（`EXAM_INVALID`/`EXAM_UNAVAILABLE`/`EXAM_REF_INVALID`），sync 失败、claim 拒绝，绝不静默回退。
- 护栏：PR diff 精确触碰该文件时，reviewer 判词评论（含 BLOCKED 的 PR 评论）由服务端追加固定警示行并记 `exam.touched_by_pr` 事件；files 查询失败则 publish 干净失败重试。
- 迁移：把机器配置 `~/.code-loop/projects/<key>/control-plane.json` 的两键原样搬进仓库文件、走 PR 合并；合并后 `status.verification_source` 从 `machine` 变 `repo` 即生效。完整语义见 `skills/code-loop/references/control-plane.md`「考题」一节。

## 代码托管平台支持（GitHub / Gitea）

服务现默认集成并原生支持 **GitHub**（REST API v2022-11-28）：
- **适配器**：`codeloop.cp.github.GitHub`（纯标准库实现，支持 Bearer 认证、API 版本锁定 `2022-11-28`、Contents 与 Raw 考题读取、状态/评审增量同步）。
- **兼容垫片**：`codeloop.cp.gitea` 维持对原有 Gitea 导入的完全向后兼容别名。
- **凭据解析**：优先读取 `GITHUB_TOKEN` 环境变量或 `~/.code-loop/credentials/GITHUB_TOKEN` 文件，平滑回退至 `GITEA_TOKEN`。
- **CLI 工具集成**：`scripts/check_ledger.py` 支持 `--github`（调用 `gh issue list`）与 `--gitea`；Operator 动作支持 `github_create_issue` / `github_comment_issue` 并保持对 `gitea_*` 别名的映射。

## 布局

```
services/code-loop/
├── README.md                 # 本文
├── codeloop/                 # 唯一包根（2026-09-16 结构收编；服务可整体 import，零安装不变）
│   ├── cp/                   # 协调器实现（runtime/store/service/github/gitea/observer）
│   │   ├── protocol.py       # server/client 共享线协议（envelope/错误码/协议版本）
│   │   ├── ws.py             # 最小 RFC6455 编解码（vendored，纯标准库；拒分片/二进制/超帧）
│   │   ├── commands.py       # 指令台账 Service 面（入队/ack/修复 sweep 的配置化封装）
│   │   ├── registry.py       # 机器注册表（machines/grants/一次性 enroll code；token 只存 sha256）
│   │   ├── worker_tasks.py   # machine-scoped typed task 台账与可重放状态事件
│   │   ├── server.py         # 数据面 HTTP app（路由白名单/Bearer/每请求 Service/worker 身份盖章）
│   │   ├── client.py         # worker 传输（TLS 钉扎/Bearer/无自动重试）
│   │   ├── observer.py       # 观察层：全系统唯一爬取路径（增量、原始 JSON 保真，CP/dashboard 共用同一个 store）
│   │   └── worker.py         # WorkerService（本地执行 + 远程协调，与 Service 同方法面；claim_raw 带 ttl/item）
│   ├── dash/                 # 面板后端（store facade + schema/ingest/verdicts/views/health 分层 + serve/collect/notify；前端源码见 frontend/）
│   ├── agent.py              # WorkerAgent：WS 常驻，校验 typed task → 本机执行 → 状态回报
│   ├── worker_task_protocol.py # task envelope/schema/action ID 严格校验
│   ├── worker_task_store.py  # worker-local command idempotency journal
│   ├── worker_task_executor.py # PR Handler 派发、诊断证据与固定维护 adapter
│   ├── agent_watch.py        # agent 的 liveness 半边（SpawnWatchMixin：spawn 启动窗口 + 进程死亡守望，2026-09-23 自 agent.py 拆出控行数；方法经混入仍是 WorkerAgent 成员）
│   ├── agent_runners/        # coding agent 插件注册表（base 接口 + claude/opencode/pi 插件）：身份白名单/模型探测/argv 预设/会话输出解析的唯一源（claude_transcript=claude JSONL，opencode_storage=opencode JSON 存储，pi_transcript=pi JSONL）
│   ├── cli.py                # 叶子模块：CLI 命令解析（Windows .cmd 桥接），trigger 与插件共用，不 import 任何 codeloop 模块
│   ├── review_round.py       # 共享预备链：claim(ttl=300) → worktree → bind（唯一执行载体 RemoteCoordinator）
│   └── trigger.py            # 事件触发实现（规则/冷却/日额/spawn）
├── frontend/                 # 面板前端源码（index.html 壳 + app.css + app.js + vendor/ 第三方库；非 Python 包，serve 按白名单只读，无构建）
├── scripts/                  # 只留入口 CLI，路径稳定（薄 skill shim 的 runpy 委托目标）
│   ├── control_plane.py      # CP JSON CLI（统一动词面 claim/bind/verify/report/publish + status/list/enable/disable + enroll/reconcile；终态由协调器推导，无 finish/heartbeat/recover；--config=协调机进程内，省略=worker 全局文件）
│   ├── cp_server.py          # CP server 管理 CLI（init/serve/enroll-code/machines/joins/requests/projects）
│   ├── dashboard.py          # 面板 CLI（collect / serve）
│   ├── worker_agent.py       # 执行机常驻 agent 入口（接指令；--server/--db 可选）
│   ├── trigger.py            # 事件触发 CLI（check / run，薄入口委托 codeloop.trigger）
│   ├── register_home.py      # 钉扎写入（见下）
│   ├── check_ledger.py       # handler 台账反坍缩校验
│   └── pending_consumption.py  # handler 待消费清单
├── prototypes/               # dashboard UI 原型（唯一设计源；硬规则见下）
└── tests/                    # 全量单测（python 直跑 + 门禁 pytest 双通道，纳入仓库门禁）
```

## Operator machine task 与本机 Coordinator

CP sync 只对新观察到的 changes-requested review / merge conflict 写 `pr.handler_repair_needed` 事件；discover 停摆按阈值写 `worker.loop_stalled` 边沿事件。中心 Operator 用持久游标消费事件并写入 machine task ledger。Operator 负责全局派发；WorkerAgent 是机器上的常驻接收/监督入口；Coordinator 是每条任务临时启动的本机智能协调者。`pr.repair` 由获授权 worker 上的 WorkerAgent 预备现有 Handler claim/worktree/bind，再启动一次性 Coordinator runtime，由它通过原生 Task/subagent 能力委派独立 Handler 完成仓库改动。`worker.project_diagnose` 由 Coordinator 分析 CP、channel、项目注册、watchdog 和已登记 scheduler 证据；它只能返回 envelope 允许的 action ID，本机代码负责确定性校验、执行固定 adapter、post-check 与审计。

Worker 的 `~/.code-loop/control-plane.json` 可设置 `coordinator_agent` / `coordinator_model` 作为机器默认，也可在 `projects.<org__repo>` 中覆盖；Dashboard 项目注册记录可覆盖 Coordinator runtime/model。`maintenance.scheduler_tasks.discover` 与 `maintenance.services.worker_agent` 是可选精确目标登记。当前固定 scheduler/service adapter 仅支持 Windows Task Scheduler / Windows service；其他平台会报告未知或拒绝相应动作，不会退化成 shell 命令。配置细节见 [Operator 契约](../../code-loop/references/operator.md#worker-coordinator-配置)。

## dashboard UI 原型机制（2026-09-17 引入，借鉴 ai_quality_analysis `design/`）

**硬规则：任何 dashboard UI 改动，必须先改/先出 `prototypes/` 下的可点击原型，经用户确认后，
才允许动 `frontend/`（index.html/app.css/app.js）等实现代码。** 原型是纯静态 HTML（无构建依赖，假数据内联），
一个视图一个文件、与实现的视图名对应；视觉 token 沿用仪表面板主题（paper/ink/petrol），
不得回退已废弃配色。机制详情与文件清单见 `prototypes/README.md`。
首个落地：`prototypes/run-detail.html` ↔ `view=run`（审核中卡片 → 运行详情实时会话流）。

## 远程接入速览（服务端 = 主开发机）

```bash
# 服务端（一次性）
python services/code-loop/scripts/cp_server.py init                 # 生成 ~/.code-loop/server/（server.json + registry）
#   非 loopback 监听：openssl req -x509 -newkey rsa:2048 -nodes -keyout cp-server.key \
#     -out cp-server.crt -days 3650 -subj "/CN=cp.your.lan"，并在 server.json 填 cert_file/key_file
python services/code-loop/scripts/cp_server.py serve
#   单例锁：daemon.lock 文件 + 内核字节锁（msvcrt/fcntl）——进程死亡即释放，
#   崩溃/重启后直接重启即可；另有 serve 在跑时第二实例拒绝（DAEMON_LOCK_EXISTS）
#   项目侧：在 ~/.code-loop/projects/<org__repo>/control-plane.json 加 "remote": true
#   （新项目完整接入 checklist——配置字段/凭据/授权顺序/冒烟——见
#     skills/code-loop/references/control-plane.md「远程模式」一节）

# worker 机（两种注册方式，二选一；注册只发机器身份，不含任何项目参数）
# 方式 A：worker 主动请求 + 服务端批准（免搬运，推荐）
control_plane.py enroll --url https://cp.your.lan:8722 --name <机器名> \
  [--interpreter <venv python>] [--verify-timeout 300]
#   worker 生成配对密钥（只传哈希）并汇报其看到的证书指纹 → 等待批准
# 服务端：cp_server.py joins pending        # 看请求；指纹与本机证书不符会标出
#        cp_server.py joins approve --name <机器名> [--as-name <正式名>]
#        （零项目批准 = 纯注册；approve 自动比对 worker 上报指纹 vs 本机证书，不符即拒绝）
# worker 侧轮询拿到 token → 自动写全局文件（只含 server.url + interpreter/verify_timeout 等
#   本机执行字段；从不自动写 server.project——项目键按运行目录 git origin 推导）
# 方式 B：operator 主动发一次性 code（适合可信任机器；--project 在服务端开码时顺带授权）
python services/code-loop/scripts/cp_server.py enroll-code --name dev-b --project org__repo
control_plane.py enroll --url https://cp.your.lan:8722 --code <一次性code> [--pin <证书sha256>]

# 项目授权（注册后的独立操作，worker 提案、服务端批准）
control_plane.py request-project                       # 在项目 checkout 里跑：键从 git origin 推导
control_plane.py request-project --project <org__repo> # 或显式指定
# 服务端：cp_server.py requests pending             # 看提案（机器名 + 项目键）
#        cp_server.py requests approve --id <n>     # 校验项目已暴露 → 写 grants
#        cp_server.py requests reject --id <n>
#   等价操作者主动通道：machines grant/ungrant --name X --project P；三入口同落 grants 表

# 之后三循环照常运行：CLI 省略 --config 即为 worker 身份，动词面与单机模式完全一致，
# 策略唯一源在服务端项目配置，claim 下发并缓存于 <key>/state/policy.json
# 接完自检：control_plane.py doctor   （连通/TLS/授权/interpreter/token 逐项打勾）

# 统一管理（服务端本机）
python services/code-loop/scripts/cp_server.py machines list        # 机器/授权/last_seen
python services/code-loop/scripts/cp_server.py machines disable --name dev-b   # 即时生效
python services/code-loop/scripts/cp_server.py machines revoke --name dev-b    # 吊销（401）
```

回滚：worker 配置删 `server` 段即回到单机模式（各自本地状态布局不变）；服务端停 serve + 项目去掉 `"remote": true` 即不再暴露。

## 调用方如何找到本服务

薄 skill 的 `skills/code-loop/scripts/{control_plane,dashboard,trigger}.py` 是薄 shim，按以下顺序定位本服务（fail-closed）：

1. 环境变量 `CODE_LOOP_SERVICE`（服务根目录；已设置但不合法 → `CODE_LOOP_SERVICE_INVALID`，不静默回退）；
2. 仓库 walk-up：从 shim 真实路径（穿透 junction）向上找 `<祖先>/services/code-loop`——主检出、任务 worktree（各用各的服务副本）、junction 部署三种形态零安装覆盖；
3. 钉扎文件 `~/.code-loop/service.home`（单行绝对路径；已存在但不合法 → `SERVICE_PIN_INVALID`）；
4. 全部未命中 → `SERVICE_NOT_FOUND`，exit 1。

## 安装 / 升级 / 回滚

- **安装**：随仓库检出即装。推荐一次性钉扎兜底（覆盖 skill 被单独拷贝的检出）：
  `python <repo>/services/code-loop/scripts/register_home.py`
- **升级**：主工作区 `git pull` 即生效（junction 链与 walk-up 都指向仓库真实路径）；dashboard serve 与 CP server 常驻进程需手动重启后才会加载新代码。
- **部署副本确定性更新（2026-09-22 起）**：部署机（本机 / GPU 测试机等）直接跑
  `python services/code-loop/scripts/update_deploy.py`——无变化秒退；落后则
  fetch → 原子锁 → `--ff-only` 拉取（本地提交/分叉/dirty 一律拒绝交人工）→
  激活门（py_compile + import 冒烟 + state_space check）→ 重启 schtasks 常驻任务
  （`code-loop-worker-agent` / `code-loop-cp-serve`，存在才操作）→ 写
  `~/.code-loop/logs/update-deploy.jsonl` 台账。退出码 0=成功/已最新，
  1=预检拒绝（未动状态），2=已拉新但门禁/重启失败（"磁盘新、进程旧"错位态，
  大声报告并附回滚指引）。`--check` 只读报告落后状态；触发为人工发起
  （同步部署副本是人的动作，自动化下发不在本工具范围）。

  > ⚠️ **重启判据是进程身份，不是任务状态**（2026-09-23 生产实测）：启动器
  > `~/.code-loop/*-silent.vbs` 用 `Run(..., 0, False)` **分离启动**，任务实例在
  > wscript 退出瞬间即结束（状态是"就绪"而非"正在运行"），python 是脱离任务实例的
  > **孤儿进程**。因此 `schtasks /end` 永远杀不掉它们（却报"成功"），任务状态与
  > `/run` 退出码都证明不了重启生效。更新器按脚本名定位真实进程 → `taskkill` →
  > `/run` → **必须观察到新 pid 才算成功**，否则报 `fail` 并以 exit 2 收尾。
- **回滚**：`git revert` 对应合并即可；未重启的旧 serve 进程与拆分前的旧 worktree 天然是过渡期回滚路径。

## 测试

```bash
python3 -m unittest discover -s services/code-loop/tests -p 'test_*.py'  # 核心服务套件（fail-closed）
python3 -m unittest discover -s tests -p 'test_*.py'                   # 仓库级集成与软链契约测试
python3 code-loop/scripts/test_shims.py                               # 技能 shim 入口测试
python3 services/code-loop/tests/test_cp_xxx.py                       # 单个测试直跑
```

### 测试分层映射

| 层级 | 落点 | 说明 |
|---|---|---|
| Unit | 各 `test_*.py` 主体（store/runtime/service/protocol 逻辑） | 控制输入下的规则与状态机行为 |
| Integration | `test_control_plane_store.py`、`test_cp_server.py`、`test_control_plane_github.py`、`test_control_plane_gitea.py`、`test_dashboard_*` | 真实边界协作：SQLite 文件库、HTTP server/client（TLS/Bearer）、git 操作 |
| E2E | 真机约定：`cp_server.py serve` 拉起（双端口）+ `worker_agent.py` 常驻 + curl 公开端点 | 非常驻自动化；合并部署后人工/轮次执行（重启 serve 后 curl 全部相关端点是既定 QA 纪律） |
| Regression | 历史缺陷复现用例（如 win32 bash 包裹、issue list 截断、outbox TEXT cp_id、serve 残留锁） | 修 Bug 先加复现测试再修，随修复入套件 |

### 基线声明（[N/A] 与已评估例外）

- **锁文件 [N/A]**：纯 Python 3.9+ 标准库 + 本地 SQLite，无可锁定第三方依赖图，生态锁文件不适用。
- **`.env` [N/A]**：凭据走 `token_env` 环境变量或机器本地 `~/.code-loop/credentials/` 文件，不入仓库。
- **lint / typecheck 已评估、暂不引入**：现有门禁（静态校验 + 全量单测）已覆盖历史真实故障模式（输出完整性、引用缺失、编码）；纯标准库代码库引入 mypy/flake8 的维护成本与当前收益不匹配。此为显式记录的例外，若后续依赖扩大须重新评估。
