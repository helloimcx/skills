# Issue #47：Handler 证据与私有日志

关联：[实施计划](../../plans/2026-11-07-issue-47-handler-evidence.md)及其方案图。

PR repair 的 Coordinator 输出经过独立的日志进程投影，只持久化最低限度的结构化证据。执行器识别 Claude `Agent` 和 `Task` 的顶层调用及配对结果，OpenCode 保留其受支持格式；Claude 的调用输入须绑定准备 execution，OpenCode 旧事件可能没有 input 或独立 session 事件（只能回退到唯一工具 session），两者均要求查询同一准备 execution 为 `SUCCEEDED`。日志与失败消息不携带原始 prompt、工具内容或 token。

WorkerAgent 继续以现有 journal 记录日志进程 PID 并按既有方式处理重启。该进程等待直接 runtime，不能证明所有脱离进程组的后代退出；进程树强隔离、CP 续租和恢复行为属于后续工作。CP API、数据库 schema 和平台路由无变化。详见 [Code Loop 模块](../modules/code-loop.md)。
