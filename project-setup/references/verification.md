# 验证契约

验证的目标是证明新项目可以被另一名工程师从干净环境可靠地安装、理解、修改、测试、构建和运行。不要只证明“生成命令退出为 0”。

## 1. 测试先行

对第一个有意义的行为切片执行：

1. **定义行为**：写出用户/调用方可观察的 Given / When / Then 或等价验收条件。
2. **RED**：先写最小测试，运行并确认它因为目标能力缺失而失败；语法错误、环境未安装或测试自身错误不是有效 RED。
3. **GREEN**：只写足以通过当前测试的生产代码。
4. **REFACTOR**：在绿色保护下改善命名、职责、重复和依赖方向。
5. **门禁**：运行最相关检查，然后进入下一个切片。

脚手架配置、文档和 CI 文件本身不需要伪造代码级 RED。对这些内容使用可执行的结构、链接、配置解析或命令契约检查。任何 Bug 修复必须先加入能复现该 Bug 的 Regression Test。

核心业务流程或复杂状态转换使用 Gherkin/BDD 描述：

```gherkin
Given a valid initial state
When the user performs the public action
Then the observable result and state transition are correct
```

BDD 场景描述行为，不与内部函数名或实现步骤绑定。

## 2. 测试层定义

每个项目都要评估四类测试，并为所有适用层级建立可发现入口；用项目形态定义“端到端”，不要因为不是 Web 项目就省略公共入口测试。某层没有真实行为或协作边界时，在 Spec/QA 中标记 `[N/A]` 并说明原因，不得为了凑齐层级而创造基础设施或无价值测试。

| 层级 | 证明内容 | 禁止替代品 |
|---|---|---|
| Unit | 单个规则或模块在受控输入下的行为 | 只调用 mock、没有业务断言的测试 |
| Integration | 两个以上真实边界协作，例如 DB、文件、序列化、路由、adapter | 与生产行为不同的手写假实现 |
| E2E | 从构建产物或公共入口完成主路径 | 直接调用内部函数冒充端到端 |
| Regression | 已发生缺陷的最小复现和永久保护 | 为尚不存在的 Bug 编造无价值用例 |

初始化时至少实现能证明最小垂直切片的 Unit 和 E2E 测试；存在两个或更多真实边界协作时实现 Integration 测试，否则明确记录 `[N/A]`。建立 Regression 的目录、标签或命令入口；在没有历史 Bug 时可以没有虚构的测试用例，但入口必须能随首个修复自然接入完整套件。

测试应确定、可隔离并能并行时不互相污染。时间、随机、网络和进程边界必须可控制；不要用任意 sleep 掩盖竞争。

## 3. 质量门禁发现与设计

优先采用语言和框架的标准工具。只加入与当前技术栈适配、能够维护的门禁。至少配置并接入：

```text
typecheck / static analysis
lint
format check
unit tests
integration tests
e2e tests
regression tests
build / package
```

### 3.1 五类硬门禁（六项可观测值）

只要项目包含可执行源码，以下五类指标就是初始化完成的硬门禁；最后一类同时统计超长函数和超大文件两个零超限计数。所有可观测值都必须测量、执行并由聚合命令报告。项目可以选择更严格的阈值，但不能降低默认阈值或仅以“以后再接入”通过：

| 门禁 | 默认阈值 | 通过条件 |
|---|---:|---|
| 圈复杂度 | 每个函数/方法 ≤ 10 | 超过阈值的函数/方法数量为 0 |
| 重复代码率 | 分析源码的 ≤ 5% | 测得重复代码率不超过阈值 |
| 循环依赖数量 | 0 | 生产依赖图中不存在循环 |
| Dead Code | 0 | 生产 Dead Code 发现数量为 0 |
| 超长函数 | 每个函数/方法 ≤ 80 个逻辑源码行 | 超过阈值的函数/方法数量为 0 |
| 超大文件 | 每个生产源码文件 ≤ 500 行 | 超过阈值的文件数量为 0 |

每个项目必须在其规范的质量配置（例如 `pyproject.toml`、`package.json`、Makefile）或 `docs/quality-gates.md` 中记录：指标定义、分析范围、排除项、工具与版本、执行命令和阈值。工具的指标口径不同于上表时，必须写出可审计的映射；不能静默替换成另一种度量。

硬门禁必须由单一 `verify`、`qa` 或 `check` 聚合入口执行。命令非零、阈值违反、分析器缺失、报告过期、结果不可复核或未经配置的排除项，都必须记为 `[FAIL]` 或 `[BLOCKED]`，不能记为 `[PASS]`。只有没有可执行源码时，才可以把这些指标标记为 `[N/A]`，并在 Spec/QA 中说明证据。生成代码或 vendor 代码只有在提交的、窄范围、可审查配置中排除；排除不能掩盖生产代码问题。

其他质量项按项目风险评估，但不得因此省略上述五项硬门禁：

```text
测试通过率与有效覆盖
静态类型与 Lint 错误
Secret / SAST
依赖与许可证漏洞
```

确保 CI 调用相同聚合入口。最终格式门禁必须是非修改型检查；自动格式化后重新检查 Git diff。

禁止通过以下方式获得绿色结果：

- 删除、skip、ignore 或弱化有效测试；
- 降低覆盖或静态规则而不解决问题；
- 加入 `any`、`nolint`、空异常处理或宽泛排除；
- 只运行修改点测试并把它称为完整测试；
- 把未安装、超时或外部服务不可用标记为通过。

除无可执行源码的 `[N/A]` 和提交配置中明确限定的 generated/vendor 范围外，任何例外都不能豁免上述硬门禁。已有问题的迁移 baseline 必须继续报告为 `[FAIL]`，直到实际结果达到阈值；不得仅凭授权、所有者或退出日期标记为 `[PASS]`。

## 4. 真实主路径

如果项目有可运行入口且环境允许，使用正式入口执行至少一个成功路径和一个关键失败路径：

- Service：启动真实服务并通过公开协议请求；
- Web：运行生产等价构建，通过浏览器完成核心路径；
- CLI：运行构建/安装后的命令，检查 stdout、stderr、退出码和文件副作用；
- Library：在隔离消费者中安装构建包，只使用公共导出；
- Worker：从真实消费入口提交消息/任务，验证状态和重复处理语义。

外部依赖无法使用时，标记 `[BLOCKED]` 并说明缺少什么。若使用本地替代环境，明确它证明和未证明的内容。

## 5. 干净环境验证

在临时目录、临时 clone 或等价隔离环境中：

1. 只使用准备纳入版本控制的候选文件和声明的工具版本；使用临时 clone 时以候选提交或等价快照为准；
2. 按 README 从零安装依赖；存在有意义锁文件时使用冻结/锁定模式，并验证未发生重新解析；
3. 运行聚合验证、build/package 和最小公共入口；
4. 确认不依赖未提交文件、本机缓存、全局包、个人凭证或绝对路径；
5. 检查生成物只包含预期文件且不含 Secrets、测试缓存和开发配置。

若环境或成本不允许完整 clean-room 验证，记录 `[BLOCKED]`，不要声称可复现。

## 6. 验收证据

为 Spec 中每条 Acceptance Criterion 记录：

| 状态 | 含义 |
|---|---|
| `[PASS]` | 已执行并附命令、测试或可检查产物证据 |
| `[FAIL]` | 已执行且结果不满足，任务不得完成 |
| `[BLOCKED]` | 因明确外部条件无法执行，说明影响和解除条件 |
| `[N/A]` | 与当前项目形态无关，说明为什么 |

推荐的最终证据摘要：

```text
[PASS] unit: <command> — <count/result>
[PASS] integration: <command> — <count/result>
[PASS] e2e: <command> — <public path>
[PASS] complexity: <command> — <max/result>
[PASS] duplication: <command> — <rate/result>
[PASS] dependency-cycles: <command> — <count/result>
[PASS] dead-code: <command> — <count/result>
[PASS] size: <command> — <long-function-count, large-file-count>
[PASS] build: <command> — <artifact>
[BLOCKED] external smoke: <missing dependency and impact>
```

## 7. 完成前自检

- 需求、边界条件、异常路径和验收标准是否真实满足？
- 模块职责、接口和依赖方向是否清晰，是否存在循环或跨层调用？
- 是否产生重复、God Class、万能工具、深层嵌套、隐式全局状态或不必要抽象？
- 配置是否集中验证，Secrets 是否与代码和日志隔离？
- 错误是否全部被处理、转换、记录或抛出，核心失败是否明确？
- trace、关键状态、外部调用、耗时、重试与降级是否足以排障且没有日志噪音？
- DB、并发、重复请求、MQ、网络、重启、迁移和部分成功的真实风险是否有明确策略？
- 输入、权限、注入、XSS/CSRF/SSRF、路径、反序列化和依赖风险是否处理？
- Unit、Integration、E2E 是否覆盖最小主路径？Bug 修复是否先有 Regression Test？复杂流程是否需要 Gherkin？
- format check、lint、typecheck、完整 tests、build 和适用安全检查是否全部通过？
- `docs/architecture.md`、Spec、Plan、ADR 和 README 是否与真实代码一致？
- 根 `AGENTS.md` 是否只保留架构影响触发器、按需策略路径和完成不变量，`CLAUDE.md` 是否在保留已有内容的同时只导入一次 `@AGENTS.md`？
- `docs/architecture/maintenance.md`、初始变更记录、diagram provider manifest、provider source/receipt 和 README 当前图是否存在且相互一致？active provider 或 fallback 是否经过真实 validation，失败是否如实标记？
- 是否残留 TODO、Mock、debug 输出、Dead Code、临时脚本或未解释例外？

正确性优先于可维护性，可维护性优先于简单性，简单性优先于未经测量的性能优化，以上均优先于交付速度。流程本身不能成为无意义复杂度的来源。
