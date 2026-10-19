# Spec: code-loop 服务集成与 Skill 规范化改造（支持 GitHub 原生适配）

## Goal

将 `code-loop` 作为机器本地确定性研发闭环服务集成到本仓库，提供面向 Agent 的对应薄 Skill，并将代码托管平台由原有的 Gitea 升级迁移为原生 **GitHub REST API (v2022-11-28)**：
1. **服务单元集成 (`services/code-loop/`)**：纯 Python 3.9+ 标准库与本地 SQLite 构建，状态流转单点化、租约超时免心跳、围栏令牌防脑裂；
2. **GitHub 原生适配**：新增 `codeloop.cp.github.GitHub` 适配器，支持 Bearer 认证、API 版本锁定、增量爬取、Commit Status 与 Check Runs 双引擎门禁判定，并保持对 Gitea 的向后兼容；
3. **薄 Skill 架构 (`code-loop/`)**：符合 skills.sh 规范，提供标准 `SKILL.md`、深度规则参考库（`control-plane.md`、`reviewer.md`、`handler.md`、`discover.md`、`operator.md`）与路径稳定的 fail-closed 委托 CLI。

---

## Scope

- 新增 `services/code-loop/`：包含 Control Plane 核心（`cp/`）、看板系统（`dash/`）、前端与原型（`frontend/`、`prototypes/`）、常驻执行机与触发器、以及全量 1390+ 单元测试；
- 新增 `code-loop/`：包含 `SKILL.md`、`references/` 规则集、`scripts/` 委托入口（`_shim.py`、`control_plane.py`、`cp_server.py`、`dashboard.py`、`worker_agent.py`、`trigger.py`、`operator.py`、`check_ledger.py`、`test_shims.py`）；
- 新增 `skills/code-loop` 软链以保证路径兼容；
- 新增 `tests/test_code_loop_repository.py` 仓库级契约测试；
- 更新 `README.md` 与 `CLAUDE.md` 索引。

---

## Non-goals

- 不修改现有独立技能（`vibe-coding`、`prototype-first`、`project-setup` 等）的内部实现。
- 不引入第三方网络库或外部进程依赖（保持纯 Python 3.9+ 标准库自包含）。
- 不破坏存量 Gitea 配置环境的平滑兼容。

---

## Behavior / Interface

### 1. 平台适配契约（GitHub Native & Gitea Compatible）
- **REST API 版本**：严格遵循 `2022-11-28` 规范，请求头带 `X-GitHub-Api-Version` 与 Bearer 令牌认证。
- **凭据解析层次**：
  1. 环境变量 `GITHUB_TOKEN`（或配置指定 `token_env`）；
  2. 机器本地凭据文件 `~/.code-loop/credentials/<token_env>`；
  3. 若 `token_env == 'GITHUB_TOKEN'`，优先检查 `GITEA_TOKEN`（向后兼容）；
  4. 若仍未获取且存在 `gh` CLI，调用 `gh auth token` 继承本机登录凭据；
  5. 全未命中且非 local 模式时 fail-closed 抛出 `GitHubError`。
- **增量爬取与 PR Simple 形态**：
  - 增量快照针对 `GET /pulls` 返回的 *Pull Request Simple* 形态，显式依据 `merged_at` 派生 `merged` 属性，解决未决关闭 PR 校验崩溃。
- **CI 门禁双引擎**：
  - `required_checks` 判定同时支持 Commit Status API 与 Actions Check Runs API（映射 `completed` + `success` 判定）。

### 2. 薄 Skill 委托契约
- **Fail-Closed 服务定位器**：
  1. `CODE_LOOP_SERVICE` 环境变量校验；
  2. 穿透 Walk-up 查找 `<ancestor>/services/code-loop`；
  3. 机器本地钉扎文件 `~/.code-loop/service.home`；
  4. 未命中以 exit 1 报错 `SERVICE_NOT_FOUND`。
- **标准脚本规范**：
  - 所有 Shim（包括 `operator.py`）具备第 1 行 shebang 与顶层 docstring，无全局 `sys.path` 污染。

### 3. 三大研发循环与运维自治
- `reviewer`：独立 worktree 检出、读取 Base commit 考题 `.code-loop/verify.json`、100% 测试通过率放行、PR 冲突检测自愈。
- `handler`：精准认领单个 Issue、TDD 红绿重构开发、反坍缩台账硬红线（`check_ledger.py` 单行检查）。
- `discover`：全局唯一爬取路径、增量事实同步入队。
- `operator`：白名单运维自愈（WAL 截断、采集刷新、命令重发）与 GitHub Issue 升级阶梯。

---

## Acceptance Criteria

1. `services/code-loop/tests/` 下所有单测 100% 通过（含 GitHub 适配器、PR Simple 回归用例与 Check Runs 用例）。
2. `code-loop/scripts/test_shims.py` 7 项服务发现与执行测试 100% 通过。
3. `tests/test_code_loop_repository.py` 仓库级完整性校验测试通过。
4. 全量仓库单元测试 `python3 -m unittest discover -s tests -p 'test_*.py'` 全部通过。
5. 根目录 `README.md` 与 `CLAUDE.md` 正确索引并说明 `code-loop`。
