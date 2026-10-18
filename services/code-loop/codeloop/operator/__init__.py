"""code-loop operator — CP 协调监控 agent（第四角色）的机器级运行面。

面向「系统本身」的订阅-处置层：常驻消费系统告警与 CP 失败事件，
L1 白名单内自治处置，危险动作升级人工。spec 见
docs/specs/2026-09-30-cp-operator-agent.md。

模块切分：
  config  — 机器配置装载（dashboard.json `operator` 节，fail-closed）
  store   — 机器级 Store（复用 cp Store 全套 schema）+ 信号工作项语义层
  ingest  — 订阅门禁（白名单/去重/冷却/日额/自消费排除/新鲜度）+ 决策台账
执行链（operator_round / act 面 / 面板）随后续 PR 落地。
"""
