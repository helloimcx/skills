# -*- coding: utf-8 -*-
"""CP 状态空间模型——声明式数据（维度、动作适用面、剪枝、规则、代码对齐清单）。

引擎在 state_space.py（纯机械）；本模块只放事实。规则来源：
references/control-plane.md 协议描述 + store.py / service.py /
command_store.py / commands.py / gate.py 的守卫逐条编纂。每条规则带一句
中文不变量断言——它就是该格子的"为什么"。

三个非格面代码桶（与 RULES 一起构成代码理由码的完整对齐面）：
- DOCUMENTED_CODES：真实守卫但不依赖格子维度（报告内容/发布闸门/载荷与
  平台事实/竞态与机器级前置），每码一条不变量；
- INPUT_VALIDATION_EXEMPT：纯入参校验，不参与状态裁决；
- UNCODED_RAISES：无理由码的 raise 点逐条裁决（输入校验 or 状态守卫缺码）。
"""
from pathlib import Path

from .state_space import Model, Rule

ACTIVE = ('RUNNING', 'VERIFYING')
ITEM_TERMINAL = ('SUCCEEDED', 'FAILED', 'BLOCKED', 'CANCELLED')
CMD_LIVE = ('PENDING', 'CLAIMED', 'SPAWNED')
UNRESOLVED = ('SENDING', 'UNKNOWN')
CLEAN = ('NONE', 'ALL_SENT')

# store.guard 的 fence 守卫没有理由码——表内以哨兵占位，check 会把它列入
# 无码守卫清单（这是枚举器的首个真实发现，见 UNCODED_RAISES）。
UNCODED_GUARD = 'EXECUTION_FENCED_UNCODED'

DIMENSIONS = {
    # work_items.state（NONE = 该 key 尚无行——put_item/claim 的候选面）
    'item_state': ('NONE', 'READY', 'RUNNING') + ITEM_TERMINAL,
    # executions.state（NONE = 执行不存在——holder 动词的 fence 面）
    'exec_state': ('NONE',) + ACTIVE + ITEM_TERMINAL[:3],
    'lease': ('NONE', 'VALID', 'EXPIRED'),
    'token': ('NA', 'MATCH', 'MISMATCH'),
    # 执行输入版本对当前版本的相对关系（NA = 无执行或已终态，moot）
    'currency': ('NA', 'CURRENT', 'SUPERSEDED'),
    # 该执行 outbox 的最坏形态（ALL_SENT = 有行且全 SENT）
    'outbox': ('NONE', 'SENDING', 'UNKNOWN', 'ALL_SENT'),
    'role': ('discover', 'handler', 'reviewer'),
    'bound': ('UNBOUND', 'BOUND'),
    # executions.result 的三态（含"报告了但未请求发布"）
    'result': ('ABSENT', 'PRESENT_NO_ACTION', 'PRESENT'),
    'branch_lease': ('NO_BRANCH', 'RESERVED_SELF', 'FREE', 'HELD_OTHER'),
    'slots': ('AVAILABLE', 'FULL'),
    'command_state': ('NA',) + CMD_LIVE + ('DONE', 'FAILED', 'EXPIRED'),
    # 指令行的 execution 关联（SPAWNED ack 落 id；终态行保留）
    'command_exec': ('NA', 'LINKED_OPEN', 'LINKED_TERMINAL', 'UNLINKED'),
    'command_attempts': ('BELOW_CAP', 'AT_CAP'),
    'command_age': ('FRESH', 'AGED'),
    # 发布意图行的回执查询结果（NA=未查询——行非 SENDING/UNKNOWN）
    'receipt': ('NA', 'FOUND', 'NOT_FOUND'),
    # bind 侧任务形态（GATE_REPAIR 无分支租约、POST_MERGE 不要求开发分支）
    'task': ('DEV', 'GATE_REPAIR', 'POST_MERGE_VERIFY'),
    # put_item 专用：新 (key,version) 对既有同 key 行的关系
    'version_identity': ('SAME', 'NEW'),
}

ACTIONS = {
    'claim': ('role', 'item_state', 'exec_state', 'lease', 'outbox', 'slots'),
    'bind': ('role', 'exec_state', 'lease', 'token', 'currency', 'outbox',
             'branch_lease', 'task'),
    'verify': ('role', 'exec_state', 'lease', 'token', 'bound'),
    'report': ('role', 'exec_state', 'lease', 'token', 'currency', 'outbox'),
    'publish_begin': ('role', 'exec_state', 'lease', 'token', 'currency',
                      'outbox', 'result', 'receipt'),
    'publish_commit': ('role', 'exec_state', 'lease', 'token', 'outbox',
                       'receipt'),
    'abandon': ('exec_state', 'lease', 'token', 'outbox', 'result'),
    'sweep_recover': ('exec_state', 'lease', 'outbox', 'result'),
    'steal': ('exec_state', 'lease', 'outbox'),
    'reconcile': ('exec_state', 'lease', 'outbox'),
    'progress_append': ('exec_state', 'lease', 'token'),
    'put_item': ('item_state', 'version_identity'),
    'command_enqueue': ('command_state',),
    'command_dispatch': ('command_state',),
    'command_ack_spawned': ('command_state',),
    'command_ack_failed': ('command_state', 'command_exec'),
    'command_sweep': ('command_state', 'command_exec', 'command_attempts',
                      'command_age'),
}

# 结构剪枝：每个不可达组合必须能说出不变量（枚举器不静默吞组合）。
# 所有谓词先确认维度在场（_has）——动作适用面不含该维度时剪枝必须惰性。
def _has(ctx, *dims):
    return all(d in ctx for d in dims)


PRUNINGS = [
    (lambda c: _has(c, 'exec_state') and c['exec_state'] == 'NONE' and any(
        d in c and c[d] != na for d, na in (('lease', 'NONE'), ('token', 'NA'),
                                            ('currency', 'NA'), ('outbox', 'NONE'))),
     '无执行则无执行域事实：租约/令牌/换代关系/outbox 一概不存在'),
    (lambda c: _has(c, 'exec_state', 'token') and c['token'] == 'NA'
     and c['exec_state'] != 'NONE',
     '令牌 NA ⇔ 执行不存在（持有者动词必持令牌调用）'),
    (lambda c: _has(c, 'exec_state', 'lease') and c['lease'] != 'NONE'
     and c['exec_state'] not in ACTIVE,
     '租约只属于活跃执行——终态化时租约即刻删除'),
    (lambda c: _has(c, 'exec_state', 'outbox') and c['outbox'] in UNRESOLVED
     and c['exec_state'] not in ACTIVE,
     '未决发布把执行钉在活跃态（limbo 红线）——永不自动终态化'),
    (lambda c: _has(c, 'exec_state', 'currency') and c['currency'] != 'NA'
     and c['exec_state'] not in ACTIVE + ('NONE',),
     '换代关系只对无执行/活跃执行有定义；终态执行 moot'),
    (lambda c: _has(c, 'result', 'outbox') and c['outbox'] != 'NONE'
     and c['result'] != 'PRESENT',
     'outbox 意图行以报告请求发布为前提：行存在 ⇒ result=PRESENT'),
    (lambda c: _has(c, 'exec_state', 'lease') and c['exec_state'] in ACTIVE
     and c['lease'] == 'NONE',
     '活跃执行必有租约（expires_at 建于领取、续于触点）'),
    (lambda c: _has(c, 'command_state', 'command_exec')
     and c['command_state'] in ('NA', 'PENDING', 'CLAIMED', 'EXPIRED')
     and c['command_exec'] != 'NA',
     'execution 关联在 SPAWNED ack 才落行；PENDING/CLAIMED/EXPIRED 必为 NA'),
    (lambda c: _has(c, 'command_state', 'command_attempts')
     and c['command_attempts'] == 'AT_CAP'
     and c['command_state'] != 'CLAIMED',
     '重试棘轮只对 CLAIMED 未 ack 分支计数'),
    (lambda c: _has(c, 'receipt', 'outbox') and c['receipt'] != 'NA'
     and c['outbox'] not in UNRESOLVED,
     '回执查询只对 SENDING/UNKNOWN 行有意义'),
    (lambda c: _has(c, 'command_state', 'command_age') and c['command_age'] == 'AGED'
     and c['command_state'] not in CMD_LIVE,
     '时间盒只作用于存活指令'),
]


def R(rule_id, action, decision, reason, invariant, when, fallback=False,
      guard=False):
    return Rule(rule_id, action, when, decision, reason, invariant,
                fallback, guard)


def _is(ctx, dim, *values):
    return ctx.get(dim) in values


RULES = [
    # ---------------- claim（领取：候选项 × 冲突阻挡者 × 容量） ----------
    R('claim-01', 'claim', 'ALLOW', None,
      '并行槽只计未过期同类活跃执行；满则空手而归（result:null 非错误）',
      lambda c: _is(c, 'slots', 'FULL')),
    R('claim-02', 'claim', 'ALLOW', None,
      '队列无可领项 = 空轮（null 与错误不可混淆）',
      lambda c: _is(c, 'item_state', 'NONE')),
    R('claim-03', 'claim', 'ALLOW', None,
      '只有 READY 项可领；项的运行态由其执行表达',
      lambda c: _is(c, 'item_state', 'RUNNING')
      or _is(c, 'item_state', *ITEM_TERMINAL)),
    R('claim-04', 'claim', 'ALLOW', None,
      '未决发布是全协议唯一人工通道——阻挡者 limbo 时候选跳过，绝不被 steal（红线）',
      lambda c: _is(c, 'outbox', *UNRESOLVED)),
    R('claim-05', 'claim', 'ALLOW', None,
      '活跃且租约有效的冲突执行保有资源；候选跳过，不顶掉在途轮',
      lambda c: _is(c, 'exec_state', *ACTIVE) and _is(c, 'lease', 'VALID')),
    R('claim-06', 'claim', 'DERIVE', None,
      'steal 即 fence：过期且 outbox 干净的受害者在同事务内 CANCELLED + 项回 READY/CANCELLED'
      '（视后继版本）+ 租约锁释放，此后受害者一切写被终态拒绝',
      lambda c: _is(c, 'item_state', 'READY') and _is(c, 'slots', 'AVAILABLE')
      and _is(c, 'exec_state', *ACTIVE) and _is(c, 'lease', 'EXPIRED')
      and _is(c, 'outbox', *CLEAN)),
    R('claim-07', 'claim', 'ALLOW', None,
      '无活跃冲突即领取：建执行（RUNNING + 单调 fencing token + 资源租约），项转 RUNNING',
      lambda c: True, fallback=True),

    # ---------------- bind（绑定：fence → 换代 → 角色只读 → 分支租约） ----
    R('bind-01', 'bind', 'REJECT', UNCODED_GUARD,
      'fencing 只查 token 与终态、从不看时钟：缺失/被接管/已终态一律拒绝'
      '（store.guard 无码守卫——建议补码，见 UNCODED_RAISES）',
      lambda c: c.get('exec_state') not in ACTIVE or c.get('token') != 'MATCH', guard=True),
    R('bind-02', 'bind', 'REJECT', 'STALE_INPUT',
      '换代 + 干净 outbox：协调器取消执行（CANCELLED）、项随新版本重建，下轮重领',
      lambda c: _is(c, 'currency', 'SUPERSEDED') and _is(c, 'outbox', *CLEAN)),
    R('bind-03', 'bind', 'REJECT', 'STALE_INPUT',
      '未决发布优先于换代——先 operator reconcile（unresolved publication）',
      lambda c: _is(c, 'currency', 'SUPERSEDED')),
    R('bind-04', 'bind', 'REJECT', 'DISCOVERY_IS_READ_ONLY',
      'discover 只读平台（只建/补 Issue）；bind 是写面动词',
      lambda c: _is(c, 'role', 'discover')),
    R('bind-05', 'bind', 'REJECT', 'DEVELOPMENT_BRANCH_REQUIRED',
      'handler 普通开发任务（DEV）必须登记分支；GATE_REPAIR 与 POST_MERGE_VERIFY'
      '例外（service.py:410/407）',
      lambda c: _is(c, 'role', 'handler') and _is(c, 'task', 'DEV')
      and _is(c, 'branch_lease', 'NO_BRANCH')),
    R('bind-06', 'bind', 'REJECT', 'BRANCH_LOCKED',
      '分支租约被他人持有时保守拒绝（BRANCH_LOCKED 兜底面）——不产生静默双写',
      lambda c: _is(c, 'branch_lease', 'HELD_OTHER')),
    R('bind-07', 'bind', 'ALLOW', None,
      '绑定即续租并预留分支租约；GATE_REPAIR 例外：无分支无 worktree 隔离、'
      '白名单命令是唯一许可效应',
      lambda c: True, fallback=True),

    # ---------------- verify（验证：fence → 绑定面；刻意不消费 currency） --
    R('verify-01', 'verify', 'REJECT', UNCODED_GUARD,
      'fence 同 bind：token/终态拒绝，与租约时钟无关',
      lambda c: c.get('exec_state') not in ACTIVE or c.get('token') != 'MATCH', guard=True),
    R('verify-02', 'verify', 'REJECT', 'BOUND_WORKTREE_REQUIRED',
      '验证前必须绑定 worktree；discover 无验证面',
      lambda c: _is(c, 'role', 'discover') or _is(c, 'bound', 'UNBOUND')),
    R('verify-03', 'verify', 'ALLOW', None,
      '过期租约不关写通道：未被 steal 前 holder 的写与在途无异；verify 不消费'
      '输入 currency——证据是本地 head 钉扎事实，currency 在消费点（report/publish）执法',
      lambda c: True, fallback=True),

    # ---------------- report（报告：fence → 换代 → 未决发布 → 落账推导） --
    R('report-01', 'report', 'REJECT', UNCODED_GUARD,
      'fence 同 bind',
      lambda c: c.get('exec_state') not in ACTIVE or c.get('token') != 'MATCH', guard=True),
    R('report-02', 'report', 'REJECT', 'STALE_INPUT',
      '换代 + 干净 outbox：执行被协调器 CANCELLED（干净 outbox 逆证无外部写），下轮重领新版',
      lambda c: _is(c, 'currency', 'SUPERSEDED') and _is(c, 'outbox', *CLEAN)),
    R('report-03', 'report', 'REJECT', 'STALE_INPUT',
      '换代但存在未决发布：先 reconcile（unresolved publication 优先于换代）',
      lambda c: _is(c, 'currency', 'SUPERSEDED')),
    R('report-04', 'report', 'REJECT', 'UNFINISHED_PUBLICATION',
      '有未决发布不得改写报告——先对账再重报',
      lambda c: _is(c, 'outbox', *UNRESOLVED)),
    R('report-05', 'report', 'ALLOW', None,
      '报告落账 + 判定迁移（闸门账本）+ 终态推导：无 action 的报告即本轮最后一步'
      '（同轮推导 SUCCEEDED）；内容校验属输入面（见 DOCUMENTED_CODES）',
      lambda c: True, fallback=True),

    # ---------------- publish_begin（prepare：fence → 报告 → 意图行） -----
    R('pb-01', 'publish_begin', 'REJECT', UNCODED_GUARD,
      'fence 同 bind（prepare 首段即 guard）',
      lambda c: c.get('exec_state') not in ACTIVE or c.get('token') != 'MATCH', guard=True),
    R('pb-02', 'publish_begin', 'REJECT', 'REPORT_REQUIRED',
      '无报告不得发布',
      lambda c: _is(c, 'result', 'ABSENT')),
    R('pb-03', 'publish_begin', 'REJECT', 'NO_PUBLICATION_REQUESTED',
      '报告未请求发布——终态已在 report 段推导，无发布面',
      lambda c: _is(c, 'result', 'PRESENT_NO_ACTION')),
    R('pb-04', 'publish_begin', 'DERIVE', None,
      '意图行已 SENT ⇒ 幂等恢复：直接推导终态并返回回执，绝不重发',
      lambda c: _is(c, 'outbox', 'ALL_SENT')),
    R('pb-05a', 'publish_begin', 'DERIVE', None,
      '完成态未知的发布查回执：找到 ⇒ 落 SENT + 终态推导（绝不重发）',
      lambda c: _is(c, 'outbox', *UNRESOLVED) and _is(c, 'receipt', 'FOUND')),
    R('pb-05b', 'publish_begin', 'REJECT', 'PUBLICATION_UNKNOWN',
      '完成态未知的发布只允许查询回执（marker）；查不到禁止盲目重发',
      lambda c: _is(c, 'outbox', *UNRESOLVED)
      and not _is(c, 'receipt', 'FOUND')),
    R('pb-06', 'publish_begin', 'REJECT', 'STALE_INPUT',
      'sync 后换代成立：执行取消，重领新版（新意图行随新轮产生）',
      lambda c: _is(c, 'currency', 'SUPERSEDED')
      and _is(c, 'outbox', 'NONE')),
    R('pb-07', 'publish_begin', 'ALLOW', None,
      'outbox 先记 SENDING 再产生外部效应；发布闸门与载荷校验属 gate 面'
      '（见 DOCUMENTED_CODES 的 ROLE_ACTION_FORBIDDEN 等）',
      lambda c: True, fallback=True),

    # ---------------- publish_commit（complete：意图行状态机） -----------
    R('pc-01', 'publish_commit', 'REJECT', 'PUBLICATION_NOT_PREPARED',
      '提交半段须先有 prepare 落账的意图行',
      lambda c: _is(c, 'outbox', 'NONE')),
    R('pc-02', 'publish_commit', 'DERIVE', None,
      '已 SENT ⇒ 幂等恢复：推导终态返回回执（不重发）',
      lambda c: _is(c, 'outbox', 'ALL_SENT')),
    R('pc-03a', 'publish_commit', 'DERIVE', None,
      'UNKNOWN 查回执：找到 ⇒ 转 SENT + 终态推导',
      lambda c: _is(c, 'outbox', 'UNKNOWN') and _is(c, 'receipt', 'FOUND')),
    R('pc-03b', 'publish_commit', 'REJECT', 'PUBLICATION_UNKNOWN',
      'UNKNOWN 查不到回执：拒绝重发，等 operator reconcile',
      lambda c: _is(c, 'outbox', 'UNKNOWN')
      and not _is(c, 'receipt', 'FOUND')),
    R('pc-04', 'publish_commit', 'REJECT', 'PUBLICATION_UNKNOWN',
      'SENDING 写路径内的 fence 失败被 except 捕获改写：行标 UNKNOWN、对外浮出'
      'PUBLICATION_UNKNOWN（写成功但提交段被接管 ⇒ 走对账，绝不重发）',
      lambda c: _is(c, 'outbox', 'SENDING')
      and (c.get('exec_state') not in ACTIVE or c.get('token') != 'MATCH')),
    R('pc-05', 'publish_commit', 'DERIVE', None,
      '单次外部写成功 ⇒ SENT ⇒ 终态推导（D2：发布是本轮最后一步）；写失败路径标 UNKNOWN',
      lambda c: True, fallback=True),

    # ---------------- abandon（持有者弃置） -------------------------------
    R('ab-01', 'abandon', 'REJECT', UNCODED_GUARD,
      'fence 同 bind',
      lambda c: c.get('exec_state') not in ACTIVE or c.get('token') != 'MATCH', guard=True),
    R('ab-02', 'abandon', 'REJECT', 'UNFINISHED_PUBLICATION',
      '可能已成功的外部写绝不自动宣告失败——operator reconcile 领地（红线）',
      lambda c: _is(c, 'outbox', *UNRESOLVED)),
    R('ab-03', 'abandon', 'DERIVE', None,
      '事实完备（报告完整且无未决发布）⇒ 推导 SUCCEEDED——弃置走 finalize 优先',
      lambda c: _is(c, 'result', 'PRESENT_NO_ACTION')
      or (_is(c, 'result', 'PRESENT') and _is(c, 'outbox', 'ALL_SENT'))),
    R('ab-04', 'abandon', 'DERIVE', None,
      '租约即刻到期走标准 sweep 推导：CANCELLED + 项回 READY/CANCELLED（视后继'
      '版本）+ 锁释放',
      lambda c: True, fallback=True),

    # ---------------- sweep_recover（claim 前置确定性回收） ---------------
    R('sw-01', 'sweep_recover', 'ALLOW', None,
      '只有活跃执行参与回收；终态/无执行无事可做',
      lambda c: c.get('exec_state') not in ACTIVE),
    R('sw-02', 'sweep_recover', 'ALLOW', None,
      '未过期不回收——过期只是回收资格，不是死刑判决',
      lambda c: _is(c, 'lease', 'VALID')),
    R('sw-03', 'sweep_recover', 'ALLOW', None,
      '未决发布永不自动终态化（红线）：保持活跃等人工 reconcile，且不占并行槽',
      lambda c: _is(c, 'outbox', *UNRESOLVED)),
    R('sw-04', 'sweep_recover', 'DERIVE', None,
      'D4：干净 outbox + 完整报告（无 action 或 action 全 SENT）⇒ 推导 SUCCEEDED',
      lambda c: _is(c, 'lease', 'EXPIRED') and _is(c, 'outbox', *CLEAN)
      and (_is(c, 'result', 'PRESENT_NO_ACTION')
           or (_is(c, 'result', 'PRESENT') and _is(c, 'outbox', 'ALL_SENT')))),
    R('sw-05', 'sweep_recover', 'DERIVE', None,
      'D5：无可用报告（或请求的发布从未 prepare——worker 死在 report 与 publish 之间）'
      '⇒ CANCELLED + 项回 READY/CANCELLED（视后继版本）自动重跑',
      lambda c: True, fallback=True),

    # ---------------- steal（claim 事务内的受害者接管） -------------------
    R('st-01', 'steal', 'ALLOW', None,
      '只有活跃执行可被接管',
      lambda c: c.get('exec_state') not in ACTIVE),
    R('st-02', 'steal', 'ALLOW', None,
      '租约有效的执行不被接管',
      lambda c: _is(c, 'lease', 'VALID')),
    R('st-03', 'steal', 'ALLOW', None,
      'limbo 受害者跳过：未决发布不被 steal（红线）',
      lambda c: _is(c, 'outbox', *UNRESOLVED)),
    R('st-04', 'steal', 'DERIVE', None,
      '接管即 fence：受害者同事务 CANCELLED + 项回 READY/CANCELLED + 租约锁释放',
      lambda c: True, fallback=True),

    # ---------------- reconcile（operator 对账——唯一人工通道） ----------
    R('rc-01', 'reconcile', 'REJECT', 'OUTBOX_NOT_RECONCILABLE',
      '只有 SENDING/UNKNOWN 可对账：SENT 幂等拒绝、无行无可对账',
      lambda c: _is(c, 'outbox', 'NONE', 'ALL_SENT')),
    R('rc-02', 'reconcile', 'DERIVE', None,
      '已核实回执落 SENT ⇒ 事实完备则同轮推导终态；operator 不受租约时钟约束',
      lambda c: True, fallback=True),

    # ---------------- progress_append（进度流） ---------------------------
    R('pg-01', 'progress_append', 'REJECT', 'PROGRESS_EXECUTION',
      '上报目标执行不存在（unknown execution）',
      lambda c: _is(c, 'exec_state', 'NONE')),
    R('pg-03', 'progress_append', 'ALLOW', None,
      '进度流无 fence：append_progress 只校验执行已知（store.py:155-175，连 token'
      '参数都没有）；按 (execution, seq) 幂等——重报忽略不重复；流是尾迹不是写通道，'
      '终态执行照常收流',
      lambda c: True, fallback=True),

    # ---------------- put_item（sync 换代落账——#239 复活面） -------------
    R('pi-01', 'put_item', 'ALLOW', None,
      '同 (key,version) 幂等返回既有行；既有行是 CANCELLED（同版本被扫废）时改写回'
      'READY（store.py:214-220，#239 复活面的另一半）——其余终态不可静默改写',
      lambda c: _is(c, 'version_identity', 'SAME')),
    R('pi-02', 'put_item', 'ALLOW', None,
      '新版本入队前旧 READY 行转 CANCELLED——一个 key 同代只有一个活口',
      lambda c: _is(c, 'item_state', 'READY')),
    R('pi-03', 'put_item', 'ALLOW', None,
      '终态行随换代复活：新版本铸新 READY 行（#239 修复语义），sync 不因终态卡壳',
      lambda c: _is(c, 'item_state', *ITEM_TERMINAL)),
    R('pi-04', 'put_item', 'ALLOW', None,
      '在途执行换代：新版本照常入队，旧执行在消费点 STALE_INPUT 自愈（不阻塞 sync）',
      lambda c: _is(c, 'item_state', 'RUNNING')),
    R('pi-05', 'put_item', 'ALLOW', None,
      '常规铸造：全新 key 落 READY 行',
      lambda c: True, fallback=True),

    # ---------------- 指令通道（commands 台账） ---------------------------
    R('ce-01', 'command_enqueue', 'ALLOW', None,
      '同目标存活指令幂等复用（created=False），不叠轮',
      lambda c: _is(c, 'command_state', *CMD_LIVE)),
    R('ce-02', 'command_enqueue', 'ALLOW', None,
      '终态/无指令 ⇒ 新建 PENDING',
      lambda c: True, fallback=True),
    R('cd-01', 'command_dispatch', 'ALLOW', None,
      '原子领取单赢家：PENDING→CLAIMED（attempts+1）',
      lambda c: _is(c, 'command_state', 'PENDING')),
    R('cd-02', 'command_dispatch', 'ALLOW', None,
      'CAS 失败返回 null——两 agent 竞速只一赢，输家不得重试',
      lambda c: True, fallback=True),
    R('cas-01', 'command_ack_spawned', 'ALLOW', None,
      'CLAIMED→SPAWNED 且携带 execution 关联（账本由此得知轮次归属）',
      lambda c: _is(c, 'command_state', 'CLAIMED')),
    R('cas-02', 'command_ack_spawned', 'ALLOW', None,
      '二次 SPAWNED ack 是 CAS 失败——execution id 已在行上',
      lambda c: True, fallback=True),
    R('caf-01', 'command_ack_failed', 'ALLOW', None,
      '#235 守卫：SPAWNED × execution 已终态 ⇒ 迟到 FAILED ack 是 CAS 失败'
      '（返回 None），绝不覆盖已完成轮次',
      lambda c: _is(c, 'command_state', 'SPAWNED')
      and _is(c, 'command_exec', 'LINKED_TERMINAL')),
    R('caf-02', 'command_ack_failed', 'ALLOW', None,
      'FAILED 落账（含 #229 liveness：SPAWNED 未终态的进程死亡回报）',
      lambda c: _is(c, 'command_state', 'CLAIMED', 'SPAWNED')),
    R('caf-03', 'command_ack_failed', 'ALLOW', None,
      '非 CLAIMED/SPAWNED（或认领者不符）一律 CAS 失败 null',
      lambda c: True, fallback=True),
    R('cs-01', 'command_sweep', 'ALLOW', None,
      '终态/无指令无事可做',
      lambda c: _is(c, 'command_state', 'NA', 'DONE', 'FAILED', 'EXPIRED')),
    R('cs-02', 'command_sweep', 'DERIVE', None,
      'PENDING 超指令 TTL ⇒ EXPIRED（COMMAND_TTL）——操作者可重试（幂等不叠轮）',
      lambda c: _is(c, 'command_state', 'PENDING') and _is(c, 'command_age', 'AGED')),
    R('cs-03', 'command_sweep', 'ALLOW', None,
      'PENDING 未过期等待派发',
      lambda c: _is(c, 'command_state', 'PENDING')
      and _is(c, 'command_age', 'FRESH')),
    R('cs-04', 'command_sweep', 'DERIVE', None,
      'CLAIMED 未 ack 且重试棘轮到顶 ⇒ FAILED(COMMAND_UNACKED) 响亮可见',
      lambda c: _is(c, 'command_state', 'CLAIMED') and _is(c, 'command_age', 'AGED')
      and _is(c, 'command_attempts', 'AT_CAP')),
    R('cs-05', 'command_sweep', 'DERIVE', None,
      '半交付回 PENDING 由修复轮重推（attempts 棘轮防无限弹跳）',
      lambda c: _is(c, 'command_state', 'CLAIMED') and _is(c, 'command_age', 'AGED')),
    R('cs-06', 'command_sweep', 'ALLOW', None,
      'CLAIMED 在 ack 窗口内等待',
      lambda c: _is(c, 'command_state', 'CLAIMED')
      and _is(c, 'command_age', 'FRESH')),
    R('cs-07', 'command_sweep', 'DERIVE', None,
      '执行行是 ground truth：轮次离开 RUNNING/VERIFYING ⇒ DONE（账本以执行事实为准，不猜）',
      lambda c: _is(c, 'command_state', 'SPAWNED')
      and _is(c, 'command_exec', 'LINKED_TERMINAL')),
    R('cs-08', 'command_sweep', 'DERIVE', None,
      '无 execution 关联的 SPAWNED 按时间盒兜底 DONE（command_spawned_window）',
      lambda c: _is(c, 'command_state', 'SPAWNED')
      and _is(c, 'command_exec', 'UNLINKED') and _is(c, 'command_age', 'AGED')),
    R('cs-09', 'command_sweep', 'ALLOW', None,
      '轮次仍在跑：LINKED_OPEN 等执行终态收口，sweep 不抢答',
      lambda c: True, fallback=True),
]

# 真实守卫但不依赖格子维度（报告内容 / 发布闸门 / 载荷与平台事实 / 竞态 /
# 机器级前置）。每码一条不变量——一致性测试要求它们在代码中真实存在。
DOCUMENTED_CODES = {
    # 机器级与同步前置
    'MACHINE_NOT_ENABLED': '未启用机器对写动词一律拒绝（读 sync/list/status 不设限）',
    'SNAPSHOT_UNKNOWN_OR_STALE': '无新鲜快照的 claim/report 无意义；report 内建自动 re-sync 自愈（#66）',
    'PROJECT_KEY_REQUIRED': 'observer store 需要项目键（服务端初始化前置）',
    'INVALID_SNAPSHOT': '平台快照形状不完整 ⇒ sync 响亮失败，不覆盖旧快照',
    # 报告内容校验（依赖 result 内容而非格子状态）
    'INVALID_REPORT_FIELDS': '报告字段白名单外键拒绝',
    'DECISION_REVIEWER_SUMMARY_REQUIRED': 'decision/reviewer/summary 必填非空',
    'INDEPENDENT_REVIEW_REQUIRED': '报告人不得是执行 worker（独立判断）',
    'DEVELOPER_CANNOT_ACCEPT_OWN_PR': '开发者不得验收自己开发的 PR',
    'INVALID_DECISION': '判定动词必须在角色期望集内',
    'BLOCKED_VISIBILITY_REQUIRED': 'BLOCKED 冲突解必须带可见性评论（非静默红线）',
    'COVERAGE_REQUIRED': 'Approved for manual merge 必带覆盖度核算',
    'COVERAGE_INCOMPLETE': 'Approved 判词的覆盖度必须 ok=true',
    # 绑定/验证的载荷与平台事实
    'PR_HEAD_MISMATCH': 'reviewer/修复绑定与验证的 head 必须逐字等于 PR head sha',
    'POST_MERGE_BASE_SHA_MISMATCH': 'POST_MERGE_VERIFY 绑定 head 必须等于 base_sha',
    'ORIGINAL_PR_BRANCH_REQUIRED': '普通 handler PR 任务必须登记原 PR 分支',
    # 发布闸门（gate.py）与载荷/平台面
    'ROLE_ACTION_FORBIDDEN': '动作必须落在角色白名单内（六动词面）',
    'ACTION_DECISION_MISMATCH': '动作与判定组合必须匹配',
    'INVALID_PAYLOAD': '发布载荷形状校验',
    'INVALID_PUBLICATION_PAYLOAD': '发布载荷形状校验（细项）',
    'INVALID_PUBLICATION_NUMBER': '发布载荷 number 校验',
    'INVALID_PUBLICATION_TEXT': '发布载荷文本校验',
    'USE_EXISTING_PR': '已有可用 PR 时不得另开新 PR',
    'WRONG_PR_TARGET': 'PR 目标不符（base/分支钉扎）',
    'ISSUE_NOT_OPEN': '目标 Issue 必须开放',
    'ISSUE_REQUIRED_NOT_PR': '该动作只接受 Issue 目标',
    'CLOSE_EVIDENCE_REQUIRED': '兜底关闭需要独立完整性证据',
    'CLOSE_TARGET_FORBIDDEN': '决策类/在途 PR 目标一律拒绝兜底关闭',
    'CLOSE_TARGET_IN_FLIGHT': '关联 PR 在途时不得关闭 Issue',
    'CLOSE_TARGET_POST_MERGE': '已合并到基线的关联 PR 走 POST_MERGE 面',
    'POST_MERGE_EVIDENCE_REQUIRED': '合并后关闭需要独立验证证据',
    'POST_MERGE_WORKTREE_REQUIRED': 'POST_MERGE_VERIFY 必须绑定 worktree',
    'POST_MERGE_VERIFICATION_REQUIRED': 'POST_MERGE_VERIFY 必须先过验证',
    'POST_MERGE_VERIFICATION_STALE': 'POST_MERGE 验证证据过期',
    'RESOLUTION_NOT_PROVEN': '判定 RESOLVED 须有既有实现覆盖证据',
    'GATE_REPORT_REQUIRED': 'reviewer Blocked 必带闸门报告',
    'INVALID_GATE_REPORT': '闸门报告形状校验（commands/env 必填等）',
    'GATE_SIGNATURE_MISMATCH': '闸门签名须与本轮 verify 证据逐字一致',
    # 发布闸门的证据前置（gate.py）
    'RUNNER_EVIDENCE_REQUIRED': '发布前必须有 runner 验证证据（agent 自报 tests_pass 不是证据）',
    'VERIFICATION_FAILED_OR_STALE': '证据必须 passed 且 head/input_version 与执行一致（钉扎不漂移）',
    'REQUIRED_CHECKS_NOT_PASS': 'PR 平台 required_checks 须全 success（mergeable 且 head 一致时才判）',
    'PR_STATE_UNKNOWN_OR_CONFLICT': 'head 漂移或 mergeable 未知/冲突 ⇒ 拒发（不基于摇晃事实发布）',
    'APPROVE_REQUIRED': 'handler 带 action 的发布判定必须是 APPROVE',
    'CONFIRMED_REQUIRED': 'discover 带 action 的判定必须是 CONFIRMED',
    'ACTION_FIELDS_MUTUALLY_EXCLUSIVE': '单动作 action/payload 与批量 actions 不能混用',
    'BATCH_ACTION_FORBIDDEN': '批量发布只允许 create_issue',
    'BATCH_PAYLOAD_TOO_LARGE': '批量发布总载荷限制大小，避免超出报告存储边界',
    'DUPLICATE_BATCH_ISSUE': '同一批次不能发布重复标题/正文的 Issue',
    'INVALID_ACTIONS': '批量 actions 必须是 2 至 10 项的列表',
    'INVALID_ACTION_ENTRY': '批量动作项必须只含 action/payload 且形状有效',
    'INVALID_ACTION_INDEX': 'Worker 只能请求当前报告中存在的批量动作索引',
    'INVALID_ISSUE_BODY_LENGTH': 'Issue 正文必须在平台允许长度内',
    'INVALID_ISSUE_TITLE_LENGTH': 'Issue 标题必须在平台允许长度内',
    'PUBLICATION_ALREADY_STARTED': '首次发布意图写入后冻结报告，避免更换已发布内容',
    'REPORT_CHANGED': '发布前报告必须与签发意图时的报告完全一致',
    'REVIEW_COMMENT_REQUIRED': 'PR 首评（无既有判词评论）必须走 review_comment 动作',
    'GATE_REPAIR_PROOF_REQUIRED': 'GATE_REPAIR 判 RESOLVED 须有绿色验证证据',
    'INDEPENDENT_POST_MERGE_REVIEW_REQUIRED': 'POST_MERGE 关闭须独立判断证据（经 _require_independent_judgment 再抛）',
    'JUDGMENT_STALE': '判定滞后于验证（report after verification）——证据新鲜度守卫',
    # 发布载荷与平台事实 / 竞态
    'PR_BRANCH_BASE_MISMATCH': 'create_pr 的 head/base 必须与执行分支/基线一致',
    'CLOSES_REFERENCE_REQUIRED': 'PR 正文必须含 Closes #n（关闭引用）',
    'PR_ALREADY_EXISTS': '同分支已有开放 PR 不得重复开',
    'PUBLICATION_IN_PROGRESS': '意图行竞态出现 ⇒ 重试走对账（不双发）',
    'PUSH_CONTEXT_CHANGED': 'push 前 worktree/branch/head 必须与绑定一致（防漂移推错）',
    'COMMAND_STATE': 'WS 派发面对 CAS 失败的服务层包装（指令非 CLAIMED 由该机器持有）',
    'EVENT_CURSOR_INVALID': '事件回放游标必须是非负整数',
    'EVENT_LIMIT_INVALID': '事件回放分页数量必须在允许范围内',
    'EVENT_SOURCE_INVALID': '事件 source ID 必须是非空字符串',
    'WORKER_STALL_THRESHOLD_INVALID': 'worker 停摆阈值必须是正数',
}

# 非理由码的机器可读字面量（补充扫描面捞到的任务/判定/fail_reason 值）——
# 记账入桶防漂移告警，但它们不是拒绝理由码。
NON_REASON_LITERALS = {
    'GATE_REPAIR': '任务形态字面量（payload.task）',
    'ISSUE_IMPLEMENTATION': '任务形态字面量',
    'POST_MERGE_VERIFY': '任务形态字面量',
    'PR_CONFLICT_RESOLUTION': '任务形态字面量',
    'PR_REVIEW': '任务形态字面量',
    'PR_REVIEW_FIX': '任务形态字面量',
    'PR_SYNC_BASE': '任务形态字面量',
    'REQUEST_CHANGES': '判定动词字面量',
    'REQUESTED_CHANGES': 'review 变化检测字面量',
    'CHANGES_REQUESTED': 'GitHub review 状态字面量',
    'NEEDS_MORE_EVIDENCE': '判定动词字面量',
    'COMMAND_TTL': 'fail_reason 字面量（sweep_commands）',
    'COMMAND_UNACKED': 'fail_reason 字面量（sweep_commands）',
    'COMMAND_FAILED': 'fail_reason 字面量（agent 侧）',
}

# 纯入参校验——不参与状态裁决（豁免须给理由）。
INPUT_VALIDATION_EXEMPT = {
    'UNKNOWN_LOOP': 'loop 参数白名单（discover/handler/reviewer）',
    'ITEM_UNKNOWN': '--item 选择器必须命中（错选响亮，不与空轮混淆）',
    'COMMAND_ACK_OUTCOME': 'ack outcome 只接受 SPAWNED/FAILED',
    'INVALID_COMMAND': '指令动作/编号入参校验',
    'PROGRESS_EXECUTION': 'execution 参数为字符串（unknown execution 变体在规则 pg-01）',
    'PROGRESS_SEQ': 'seq 必须为正整数（幂等键的一半）',
    'REMOTE_RECEIPT_REQUIRED': 'reconcile 必须带非空回执对象',
}

# 无理由码 raise 点逐条裁决：'input' = 入参/程序员断言可接受；
# 'guard' = 状态守卫缺码——枚举器的发现，修复方向是补码。
UNCODED_RAISES = {
    'resources must be a non-empty list': 'input：资源列表入参',
    'invalid resource': 'input：资源字符串形状',
    'path resource must be a normalized': 'input：path 资源归一化',
    'only path:* wildcard is supported': 'input：path 通配白名单',
    'resource must be issue-, pr-, branch-, gate-, op:, or path:': 'input：资源前缀白名单（op: 为 operator 信号项，2026-09-30）',
    'nested transactions are not supported': 'input：事务嵌套程序员断言',
    'key, kind and version must be non-empty strings': 'input：put_item 三键',
    'items cannot be inserted as active executions': 'input：插入态白名单',
    'ttl must be a finite positive number': 'input：TTL 正有限数',
    'worker and positive integer limit are required': 'input：claim 参数',
    'fencing token must be an integer': 'input：令牌类型',
    'finalize_locked requires an open transaction': 'input：事务内调用断言',
    'not a mapping': 'input：result JSON 形状',
    # 唯一的状态守卫缺码——建议补码（如 EXECUTION_FENCED）后从本清单移除
    'execution is missing, fenced, or terminal': (
        'guard：全协议最高频守卫（每个 holder 动词的 fence 面）无理由码——'
        '表内以 UNCODED_GUARD 哨兵占位（bind-01/verify-01/report-01/pb-01/'
        'ab-01 五处引用；progress 无 fence、publish 提交段的 fence 被 except 改写'
        '为 PUBLICATION_UNKNOWN 故引用实名）'),
}

def disposition_for(message):
    """无码 raise 消息 → 裁决注记（前缀匹配）；未裁决返回 None。"""
    for prefix, note in UNCODED_RAISES.items():
        if message.startswith(prefix):
            return note
    return None

_CP_DIR = Path(__file__).resolve().parent
REVIEW_PATH = _CP_DIR / 'state_space_review.json'
_SCAN_FILES = ('store.py', 'service.py', 'command_store.py', 'commands.py', 'gate.py')


def build():
    return Model(dimensions=DIMENSIONS, actions=ACTIONS,
                 prunings=PRUNINGS, rules=RULES)


def load_review():
    from .state_space import load_review as _lr
    return _lr(REVIEW_PATH)


def scan_cp_sources():
    from .state_space import scan_code_reason_codes
    return scan_code_reason_codes([_CP_DIR / name for name in _SCAN_FILES])
