# 架构治理初始化

本文件只在初始化或为已有 bootstrap 项目增量补齐架构治理时读取。目标是让后续 Codex 与 Claude Code 任务能从短规则判断是否需要维护架构，再按需读取详细流程；不要把完整维护流程复制进常驻 Agent 指令。

## 1. 先保护已有内容

在写入前检查根目录的 `AGENTS.md`、`CLAUDE.md`、README、`docs/architecture.md` 和现有 `docs/architecture/`：

- preserve 所有无关内容、项目约定、嵌套规则和人工维护说明；
- 只在仓库根目录建立共享路由，除非现有 monorepo 规则明确要求更窄作用域；
- 识别已有等价规则、导入和 managed markers，执行 idempotent 合并，不重复追加；
- 若已有规则与本契约冲突，先保留更严格且与项目事实一致的规则，再在初始化证据中说明合并结果；
- 不把用户级 Agent 配置、全局 skill 安装或个人绝对路径写入仓库。

## 2. 建立工具无关的架构事实与分层规范矩阵

`docs/architecture.md` 是当前架构的事实入口，而不是某个绘图工具的说明书。至少记录真实存在的：

- 系统目标、公共入口和主要运行路径；
- 组件或重要模块的稳定标识、职责和源码证据；
- 依赖方向、公共协议、同步/异步关系；
- 数据所有权、存储与主要数据流；
- 外部集成、部署边界和信任边界；
- 当前图、provider manifest 和变更历史的位置。

### 2.1 L1-L2-L3 分层架构规范矩阵 (Architecture Matrix)

复杂系统应采用分级 Architecture-as-Code 规范，避免将全系统细节堆叠在单张图上。规范文件统一存放在 `docs/architecture/`，支持版本化与静态校验：

- **L1 - 系统架构 (System Architecture)**：系统顶层分层职责、主要模块、数据存储、跨边界与网络/部署拓扑（推荐命名：`system-architecture.json` 或 `system.architecture.json`）。
- **L2 - 关键工作流与时序 (Workflows & Sequences)**：
  - 核心业务处理工作流（推荐命名：`*.workflow.json`，如调度、分发、流水线编排）。
  - 关键协议交互与跨进程通信时序（推荐命名：`*.sequence.json`，如会话握手、协议协商、消息生命周期）。
- **L3 - 核心实体生命周期 (Lifecycles & State Machines)**：核心领域实体或执行任务的有限状态机、状态流转、守卫条件与动作（推荐命名：`*.lifecycle.json`）。
- **全景矩阵大盘 (`docs/architecture/overview.md`)**：以结构化表格建立 L1-L3 架构全景矩阵索引，集中关联各层级规范源文件、对应交互 HTML 画布、浅深色渲染图与深度设计文档，形成双向导航。

创建 `docs/architecture/changes/YYYY-MM-DD-bootstrap.md` 作为初始基线记录，说明初始化时实际建立的组件、边界、provider 和证据。后续每次 `Architecture Impact: Required` 追加一个独立语义变更记录；不要在当前架构文档中累积过期版本。

将 [architecture maintenance policy asset](../assets/architecture-maintenance.md) 复制或按项目既有术语做最小适配，生成 `docs/architecture/maintenance.md`。不得删除其中的触发判定、事实/历史边界、provider 可替换性、README 同步、验证和失败语义。

## 3. 写入短 Agent 路由

在根 `AGENTS.md` 中创建或更新以下 managed block。常驻内容只保留触发器、按需加载路径和完成不变量：

```md
<!-- project-setup:architecture-maintenance:start -->
## Architecture maintenance

Before implementation, classify Architecture Impact as `Required` or `None`.
Use `Required` when a change alters module or service responsibilities,
dependency direction or public protocols, data ownership or flow,
trust, deployment, process, or network boundaries, external integrations,
synchronous/asynchronous communication, or adds, removes, splits, or merges
an architectural component.

When `Required`, read and follow `docs/architecture/maintenance.md` before
implementation. Completion requires the current architecture facts, one
change record, provider artifacts and validation, and the README diagram to agree.
<!-- project-setup:architecture-maintenance:end -->
```

不要把 provider 命令、历史模板、README 模板或长验证清单放进该 block。

在根 `CLAUDE.md` 保留已有 Claude 专属说明，并确保存在一次共享导入。若根 `CLAUDE.md` 与 `.claude/CLAUDE.md` 都不存在，创建只包含共享导入的最小根文件；若任一受支持文件已经存在，更新现有文件而不为了统一路径覆盖或搬迁它。若项目只有 `.claude/CLAUDE.md`，这是为了 preserve 现有 Claude Code 规则的 compatibility exception，应在初始化证据中记录实际位置：

```md
<!-- project-setup:shared-agent-rules:start -->
@AGENTS.md
<!-- project-setup:shared-agent-rules:end -->
```

若文件已经在其他位置导入 `@AGENTS.md`，不要再添加第二次。若仓库只有 `.claude/CLAUDE.md` 且项目规则要求继续使用该位置，在那里添加导入并记录实际位置；不得为了统一路径覆盖原文件。

## 4. 生成 replaceable provider manifest

创建 `docs/architecture/diagram-provider.yaml`。它是 provider 选择和稳定输出路径的声明，不是架构事实来源。使用下列最小字段形状，并按实际可用能力填写：

```yaml
schema_version: 1
provider:
  preferred: archify
  active: archify
  fallback: mermaid
capabilities:
  validate: required
  render_readme: required
  compare: preferred
outputs:
  readme: docs/architecture/system-architecture.svg
  interactive: docs/architecture/system-architecture.html
  history: docs/architecture/changes
provider_files:
  source: docs/architecture/providers/archify/system.architecture.json
  receipt: docs/architecture/providers/archify/system.receipt.json
readme:
  mode: static-image
```

当 `active` 为没有机器收据的 fallback（例如 Mermaid）时，仍须填写 active provider 的 source 路径，并将 `receipt` 明确设为 `null`；此时 validation 证据写入变更记录。不要保留指向未使用 provider 的路径。

例如 Archify 不可用时，manifest 可以改为：

```yaml
schema_version: 1
provider:
  preferred: archify
  active: mermaid
  fallback: null
capabilities:
  validate: required
  render_readme: required
  compare: not-supported
outputs:
  readme: docs/architecture/system-architecture.svg
  interactive: null
  history: docs/architecture/changes
provider_files:
  source: docs/architecture/providers/mermaid/system.mermaid
  receipt: null
readme:
  mode: inline-mermaid
```

字段值必须描述真实状态：

- 用户已指定 provider 时保留用户选择；
- Archify 已可用时可将其设为 `active`，provider 专有文件放在 `docs/architecture/providers/archify/`；
- Archify 不可用且用户未授权安装时，将 `active` 设为实际 fallback，例如 `mermaid`，并删除不适用的 Archify 专有路径，不得谎报 Archify 输出；
- provider 改为其他工具时，仅改变 manifest、`docs/architecture/providers/<provider>/` 和生成机制；`docs/architecture.md`、历史格式、Agent 路由与稳定 README 输出路径不随之改变；
- 若 active provider 只能生成 inline Mermaid，将 `readme.mode` 设为 `inline-mermaid`，仍保留 manifest 中稳定的 `outputs.readme` 逻辑路径作为未来静态导出的保留位置，但本次 README 不引用该路径；将 `provider_files.source` 指向 `docs/architecture/providers/mermaid/` 下的真实源文件，不要留下不存在的静态图片链接。provider 替换时不得改变这个稳定路径。

Archify（[repository](https://github.com/tt-a1i/archify)）是首选画图 provider，不是 hard dependency。若已安装，先在执行时发现实际 skill/CLI 位置和版本，再遵循其自身 skill 的当前说明完成 source authoring、validation、可信交付、静态 README 导出和可用时的 Architecture Delta；将实际 provider、版本、输入和 validation/delivery receipt 记录到 provider receipt 或变更记录。不得静默安装 Archify、修改全局 Agent 配置，或在 validation 失败后覆盖 last-known-good 产物。

### 4.1 自动化架构防腐门禁 (lint:arch)

为防止架构规范与源码实现脱节、语法损坏或布局退化，项目应建立自动化架构门禁（如 `scripts/lint-architecture.mjs` 或语言原生对应脚本）：

1. 自动发现 `docs/architecture/` 下所有架构规范：`system-architecture.json`、`*.workflow.json`、`*.sequence.json`、`*.lifecycle.json`；
2. 当 active provider 为 Archify 时，调用 `archify validate <type> <file> --quality showcase` 检验规范 Schema 合法性与 9 项展示级质量指标（节点无重叠、连线无交叉、标签对齐、流向一致、主题自适应等）；
3. 在项目规范命令（如 `package.json` 的 `"lint:arch"`、Makefile 或 pyproject.toml）中提供入口，并将其纳入 CI 门禁与聚合验证（`verify` / `qa` / `lint:gates`）；
4. 任何规范解析错误、Showcase 违规或图表陈旧均直接退出非零码，阻断集成。

## 5. 初始化 README 架构区块

在根 README 中只创建或更新一次：

```text
<!-- project-setup:architecture-diagram:start -->
<!-- project-setup:architecture-diagram:end -->
```

在两个 marker 之间写入项目实际的架构标题、当前静态图或 inline Mermaid，以及当前架构事实、全景矩阵大盘和变更历史链接。

静态图必须使用 manifest 中声明的仓库相对路径（推荐使用 `<picture>` 标签分别自适应深色与浅色主题，如 `.dark.png` 和 `.light.png`，或 SVG）并真实存在；交互 HTML 画布作为附加探索链接，不能替代 README 中可直接看到的当前图。fallback 为 Mermaid 时直接在 managed block 内保留与当前架构一致的单张图。不得覆盖 README 的其他章节。

## 6. 初始化验收

完成前验证：

- 根 Agent 路由短小、唯一、指向存在的 `docs/architecture/maintenance.md`；
- Claude Code 能通过一次 `@AGENTS.md` 导入获得同一规则；
- `docs/architecture.md`、`docs/architecture/overview.md` 全景矩阵、bootstrap history、manifest、provider source/receipt 与 README 描述相互一致；
- L1-L3 架构规范矩阵文件存在，命名符合规范（`system-architecture.json`、`*.workflow.json`、`*.sequence.json`、`*.lifecycle.json`）；
- 自动化架构门禁 `lint:arch` 执行通过（0 错误、0 警告）；
- README managed block 唯一，静态链接或 `<picture>` 资源存在，或 inline Mermaid 可解析；
- active provider 的必需 validation 成功；使用 fallback 时 manifest 和证据如实反映；
- provider 不可用、导出失败或产物陈旧时记录 `[BLOCKED]` 或 `[FAIL]`，不得标记 `[PASS]`；
- 初始化不依赖 `project-setup` 在后续开发任务中再次被调用。
