---
name: prototype-first
description: >
  原型先行（原型设计）：任何 UI / 页面 / 界面改动前，先在项目 design/（或 prototypes/）目录
  产出可点击的静态高保真原型（HTML+CSS+原生 JS、假数据内联、无构建依赖、浏览器直接打开），
  经用户确认后才允许动实现代码；提供 design/ 目录初始化模板（README 清单表带状态列：
  待确认→已确认→已确认并实现）、原型页骨架模板，以及 check_design.py 清单一致性校验脚本。
  当用户说"先出原型"、"出个原型"、"原型设计"、"高保真原型"、"设计稿"、"原型确认"、
  "初始化 design 目录"、"落地原型流程"，或任何前端 / dashboard UI 改动开发前使用。
---

# prototype-first — 原型先行 UI 开发流程

**硬规则（批准门，不可跳过）：任何 UI 改动必须先改/先出 `design/` 下的可点击原型，经用户确认后，才允许动实现代码。** 未确认前写实现代码一律返工。

为什么：UI 改动的成本大头是「做完才发现交互不对」的返工。文字方案暴露不了交互问题；可点击原型用假数据把布局、状态、边界情况全部演出来，用户点过、确认过，实现照着做，返工最少。本流程源自 ai_quality_analysis `design/` 的生产实践（硬规则 1），已在 code-loop dashboard 复用验证。

## 三段工作流

### 1. init（项目首次落地）

1. 与用户确认原型目录名（默认 `design/`；已有惯例如 `prototypes/` 也可）。
2. 建目录：`README.md`（抄 [references/template-README.md](references/template-README.md)，填清
   单表）+ `assets/`（共享 css/js）。
3. **把硬规则写进项目 `AGENTS.md` / `CLAUDE.md` 硬规则区**（一行）：
   > `design/` 是唯一 UI 原型源：任何 UI 改动必须先改原型、经用户确认后才启动开发。
   不写进项目指令，下一个会话就不知道有这道门——这一步是流程跨会话生效的关键，不可省。
4. 跑 `python <skill目录>/scripts/check_design.py <design目录>` 确认 exit 0。

### 2. per-change（每次 UI 改动）

1. **改原型**：新页抄 [references/template-page.html](references/template-page.html)；改既有页直接
   改；多视图大项目在「演进基座」完整副本上演示增量（见 conventions §9）。假数据内联页面
   `<script>`，视觉 token 与实现主题一致。
2. **自检**：`python scripts/check_design.py <design目录>`——清单与磁盘不一致即红，先修。
3. **请用户确认**：起 `python -m http.server 8765` 给地址，或发截图。**停在批准门等确认。**
4. 确认后：README 状态列 `待确认 → 已确认`；spec/plan 的「关联」字段写明原型文件与确认事实。
5. 进入实现（走项目常规开发流程）；实现落地后回填 `已确认并实现（日期 + 对应实现入口）`。

### 3. check（改动前后各一次）

```bash
python scripts/check_design.py <design目录> [--quiet]
```

FAIL（exit 1）：缺 README / 缺清单表 / 缺「文件」或「状态」列 / 孤儿原型（盘上有清单无）/
幽灵行（清单有盘上无）/ 重复登记行 / 状态词汇非法 / 清单行「文件」列解析不到 .html 文件名。
WARN（不阻断）：页面缺 `<!-- proto:` 头注释 / README 引用 assets 未带 `?v=` 版本 pin。

## 状态词汇表（README 状态列唯一合法值）

| 状态 | 含义 |
|---|---|
| `待确认` | 原型已出/已更新，等用户看 |
| `已确认` | 用户拍板，开发可启动 |
| `已确认并实现` | 已落地（附日期）；原型保留为设计事实源 |

状态值后允许附括号备注（日期、走查结论等），校验只认前缀。

## 红线

- 不跳过原型直接写实现；不用文字方案/截图替代可点击原型。
- 原型确认后不删除：它是该视图的设计事实源，后续改同一视图仍先改原型。
- 原型目录是设计资产不是运行时代码：serve 不路由它，测试不加载它。
- 完整约定（命名、assets 版本 pin、演进基座、口径同步、与外部设计工具解耦）见
  [references/conventions.md](references/conventions.md)。
