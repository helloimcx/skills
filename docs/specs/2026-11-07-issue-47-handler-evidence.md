# Issue #47：Handler 完成证据

状态：用户于 2026-11-08 同意收敛范围。基线：origin/main c82cdf915e3c1ca060e95a91d5fb72b3f3eac901。

## 目标和证据

正确识别 Claude 原生 `Agent` 子任务的完成事件，保留历史 `Task` 与 OpenCode 的受支持格式，并要求对应的 CP execution 已成功。只读检查的 11 份脱敏历史结构均包含 `Agent` 调用及同 ID 的工具结果；关联 execution 均已取消，不能推断历史任务本可成功。

## 范围与行为

- 按工具调用 ID 匹配顶层 `Agent` / `Task` 与结果。`Agent` 需要明确的 completed 状态；后台、失败、未完成、重复冲突、多顶层调用、嵌套调用和未知格式均不得误判为成功。
- Claude 子调用输入绑定到精确的准备 execution，且查询返回的同一 execution 为 `SUCCEEDED`；保留现有 fencing token。
- OpenCode 保持协调者 session 与子调用的既有完成格式；旧事件缺少独立 session 事件时回退到唯一工具事件 session，旧版未提供 input 时保留兼容。这两种回退不能独立证明调用处于顶层或输入绑定。提供 input 时须绑定准备 execution；所有情况均须查询同一准备 execution 为 `SUCCEEDED`。
- PR repair runtime 输出实时投影为只含证据结构的本地 0600 日志；不持久化工具内容、原始 prompt 或 token。错误和成功摘要不包含这些内容。诊断包含稳定原因码与安全的任务、execution、日志引用。
- 保留既有 worker 的任务恢复、工作区清理、租约与运行方式；macOS 原生运行不增加路由或 VM 要求。

## 不在范围

进程树强隔离、崩溃后所有后代的原子追踪、CP 租约续期与自动安全恢复，由 [Issue #52](https://github.com/helloimcx/skills/issues/52) 单独处理；当前已有的进程生命周期风险仍存在。不得宣称所有后台后代均已结束。XPC 原型验证中，脱离进程组的双重 fork 后代在 XPC 服务退出后仍然存活，无法解决该问题。

## 验收

- [x] 脱敏 Claude `Agent` 配对及旧 `Task`、OpenCode 已支持格式正确识别；缺失、可识别的嵌套、未完成、失败、重复冲突、多调用及未知格式不能误判成功。旧版 OpenCode tool-only session 回退无法独立证明顶层身份，作为兼容限制记录。
- [x] 仅同一已准备 CP execution 的 `SUCCEEDED` 允许成功，其余状态或读取失败安全失败。
- [x] PR repair 日志、远程错误和结果不包含原始 prompt、工具内容或凭据；本地日志权限受限。
- [x] 保持现有任务运行和恢复契约，不引入跨平台子进程强隔离声明、数据库迁移、CP 新端点或平台禁用。
- [x] 相关测试、独立复审和可执行的隔离入口验证完成；真实 runtime 与跨平台验证限制见下。

## QA（2026-11-08）

- 独立复审发现并修复 Windows 直接 runtime 超时、OpenCode 旧事件兼容、claim journal 错误脱敏、非 PR 诊断回归、后台继承 stdout、大 prompt 阻塞和恢复 PID 生命周期等问题；修复后复审无阻塞项。
- code-loop 服务全量 unittest：1529 通过、2 跳过。全量运行期间补充了 CP 未知状态脱敏，之后受影响的证据与执行器测试分别再次通过（24、10 项）。仓库级 unittest 19 通过，shim 测试 7 通过。
- 隔离 subprocess 的 PR repair 日志和真实执行器入口通过；三次冒烟耗时 0.046、0.046、0.062 秒，不包含模型服务。没有连接真实 Claude/OpenCode 或执行生产任务；Windows 超时由模拟验证，未在 Windows 主机运行。
- 系统 Python 没有 pytest；按仓库支持的 unittest 入口完成测试。本仓库未配置适用的 JSON 架构 schema 或 `lint:arch`。
