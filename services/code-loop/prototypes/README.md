# prototypes/ — dashboard UI 原型（唯一设计源）

借鉴 ai_quality_analysis 项目 `design/` 的原型机制（2026-09-17 引入）：

> **硬规则：任何 dashboard UI 改动，必须先改/先出本目录下的可点击原型，经用户确认后，才允许动 `frontend/`（index.html/app.css/app.js）等实现代码。**

## 为什么

dashboard 的改动成本大头在「做完才发现交互不对」的返工。静态描述（文字方案）无法暴露交互问题；
可点击原型用假数据把布局、状态、边界情况全部演出来，用户点过、确认过，实现照着做，返工最少。

## 约定

- 纯静态 HTML + CSS + 原生 JS，**无构建依赖**，浏览器直接打开即用；假数据内联在页面 `<script>` 里。
- **演进基座（2026-09-30 起，用户拍板）**：`index.html` 是完整 dashboard 可交互副本（相对引用真实
  `../frontend/app.css` + 八视图壳 + hash 路由 + 可点击交互）。**此后所有原型修改都基于该完整页面进行**——
  新视图/交互改动直接加在 index.html 上演示（含跨视图联动与深链走查），经用户确认后才动 `frontend/`；
  不再新增单视图迷你原型。存量单视图文件保留为各自已实现视图的设计事实源（改同一已实现视图时以
  index.html 为准出增量，存量文件不再演进）。
- 一个视图/一次改动一个文件，命名与实现的视图名一致（如 `run-detail.html` ↔ `view=run`）。
- 视觉 token 与 `frontend/app.css` 仪表面板主题一致：`--paper:#f4f6f8`、`--ink:#16232e`、
  `--acc:#0e7490`（petrol）、`--ok/--err/--warn`；不得引入苹果蓝等已废弃配色。
- 原型页顶部注释写明：对应需求、入口路径、以及它定义的数据契约（若新增后端接口）。
- 原型确认后不删除：它是该视图的设计事实源，后续改同一视图仍先改原型。
- 原型目录随服务源码走 git（`services/code-loop/prototypes/`），但**不是**运行时代码：
  serve 不路由它，测试不加载它。

## 文件清单

| 页面 | 文件 | 状态 | 对应实现 |
|---|---|---|---|
| operator 干预台账 + 手动触发协调巡检对话框（**演进基座**） | `index.html` | 待确认（2026-09-30 v3：由 operator-ledger.html 更名定基——完整 dashboard 副本八视图全可点，浏览器实测通过；样式相对引用 `../frontend/app.css` 零漂移；**运行详情复用既有 view=run 组件（run-detail.html，bind=轮日志档，零新增设计）**；spec：2026-09-30-cp-operator-agent）；**2026-10-09 增量已确认并实现**：看板去「已合并」列（7→6 列，折叠交互一并删）+ 可视宽度 1400→1760px + 仪表面板美化（列=stage 色微染仪表通道/轨道 4px/列头标尺线/卡片内距字级/chip 脆化/读数条数字加粗）；**2026-10-10 增量已确认并实现**（issue #314/#315 响应性修复）：采集异步单飞三态（立即采集 → 已提交·采集中… → 复位，POST /api/collect 立即返回 + GET /api/collect/status 轮询）+ 切视图卸载非活动视图 DOM、切回自动重载 | `view=operator` + `GET/POST /api/operator*` + view=run 深链 `operator:<32hex>` + 循环页/告警条触发入口；kanban 列与样式落 `frontend/app.js KANBAN_COLS` + `frontend/app.css`（main/kanban-col/card/chip）；采集三态落 `frontend/app.js collectStart` + `codeloop/dash/serve.py start_collect` |
| 执行历史（循环轮次 + 子代理档案统一列表） | `execution-history.html` | 已确认并实现（2026-09-21 用户拍板「开工」；落地时增补：项目筛选下拉、同父会话任务组折叠，均数据驱动） | `view=history` + `GET /api/history`（spec：2026-09-21-dash-execution-history） |
| 有冲突 PR 触发修复（repair_round 指令 + 处理中实时执行卡 + view=run 入口） | `conflict-repair-trigger.html` | 已确认并实现（2026-09-20；走查追加：范围外 base≠CP base 的卡不给修复按钮） | 触发修复按钮 + POST /api/repair/trigger + 处理中列 live 投影（feature/mocx/dash-conflict-repair-trigger） |
| 触发审核选 agent 与模型（agent 卡片由插件注册表下发） | `review-trigger-options.html` | 待确认（2026-09-20 原型已更新：远程能力上报——scope 来源标注 + 执行机视角空态，四条路径；载荷 agent/model 已随 PR#209 落地，另 数据驱动渲染改造待确认，见 2026-09-18 agent-runner 重构） | 触发审核对话框 + `GET /api/spawn/options?project=`（scope 组装，spec：2026-09-20-remote-agent-capability） |
| 运行详情（审核中实时会话流 + CP 时间线） | `run-detail.html` | 已确认并实现（2026-09-18 绑定来源矩阵已随前序 PR 实现；2026-09-21「子代理执行」变体（agent-session 子会话直读、档案头部/时间线/凭证）——执行历史 agent 行复用本组件，独立 agent-run-detail 原型同日废弃删除） | `view=run`（看板「审核中」/执行历史行点击）+ `runapi`/`historyapi`/`frontend/app.js:runBindChip` |
| 看板 review 触发反馈（失败警示条 + 按钮三态 + 范围外态） | `review-trigger-feedback.html` | 已确认并实现（失败条/预检三态已合并 PR#210；范围外态自 2026-09-18 起待确认） | 看板 PR 卡 `trigger` 字段 + `/api/review/preflight`（spec：2026-09-18-review-trigger-feedback）+ `review_scope`（spec：2026-09-18-dash-trigger-feedback） |

## 本地预览

直接双击打开任一 HTML；或起静态服务后批量浏览：

```bash
cd services/code-loop/prototypes && python -m http.server 8766
```
