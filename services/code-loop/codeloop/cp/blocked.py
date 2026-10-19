"""handler 同因 BLOCKED 升级/冷却的确定性判定（issue #280）。

现象（生产实证）：repair:617 连续 31 轮同因 BLOCKED——每轮一个新 LLM 会话
重新发现同一事实（原分支被外部 worktree 占用），系统无升级、无冷却、看板
静默空转。本模块提供四项纯函数判定：

- ``fingerprint(result)``：从 handler 的 BLOCKED report 提取**确定性原因
  指纹**。主判据是 summary 里的稳定原因码：handler 显式输出 ``reason=`` /
  ``原因=`` token，或 ``BLOCKED: <原因>`` 中的第一语义段（括号细节、日期
  剥除，尾部措辞漂移不断链）；PR 号只作前缀维度、**不单独成指纹**——同 PR
  不同原因的 BLOCKED 指纹不同，不再误累计（评审 MAJOR-3）。
- ``consecutive()``：给定某 key 的最近执行历史，从尾部向前数**连续同指纹**
  BLOCKED 次数；非 BLOCKED 终结或指纹变化即中断，未终结（RUNNING/CANCELLED/
  FAILED/脏数据）行中性——不计数也不打断（评审 MINOR 语义已测试固化）。
  达到 ``limit`` 触发升级/冷却判定。
- ``cooldown_events()``：对比相邻两轮 sync 的冷却集合，产出「进入冷却 /
  恢复 / 原因变化」**翻转事件**——冷却持续期不再刷事件（评审 MINOR）。
- ``probe_due()``：冷却 key 的探针节拍。冷却触发后每间隔 ``interval`` 派生
  一次探针项重新检验环境（评审 BLOCKER-1：原实现冷却后不再派生 → 永不产生
  新 execution → 连续计数永不断 → 环境变化永远无法解除，结构性死锁）。
  环境恢复后探针产生非 BLOCKED 终态 → 计数归零 → 冷却自然解除。
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple


# 升级阈值（issue 建议 N=3：连续 3 轮同因 BLOCKED → 冷却 + 事件留痕）
ESCALATION_LIMIT = 3

# 冷却后探针重试的默认间隔（秒）；可用配置项 blocked_probe_seconds 覆盖。
DEFAULT_PROBE_SECONDS = 3600.0


def _reason_token(summary: str) -> Optional[str]:
    """从 BLOCKED summary 提取稳定原因 token；无则返回 None。

    - 显式原因码（handler 固定输出根因 token）：``reason=<code>`` /
      ``原因=<code>`` —— 措辞无论如何漂移都不影响归因。
    - ``BLOCKED: <原因>`` 行的第一语义段：剥掉括号（路径/编号等细碎事实）
      与日期时间戳，尾部漂移不断链，头部仍是原因主句。
    """
    if not summary:
        return None
    explicit = re.search(r'(?:reason|原因)\s*[:=]\s*([0-9A-Za-z][0-9A-Za-z_\-/]+)',
                         summary, re.IGNORECASE)
    if explicit:
        return 'code:' + explicit.group(1)
    match = re.search(r'^\s*BLOCKED(?::|\s+)\s*(.+)$', summary, re.MULTILINE)
    if not match:
        return None
    text = match.group(1).strip()
    # 剥括号：worktree 路径、PR 号、占用者等可变的细碎事实
    text = re.split(r'[（(]', text, 1)[0].strip()
    # 剥行尾日期时间戳（如 17:32:11 / 2026-09-28），措辞漂移不断链
    text = re.split(r'\s+\d{4}[-/]\d{1,2}[-/]\d{1,2}', text, 1)[0].strip()
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return None
    return text[:120] or None


def fingerprint(result: dict) -> str:
    """从 BLOCKED report 提取确定性原因指纹。

    PR 号只作前缀维度（区分任务身份），主判据永远是原因 token：
    * 同一 PR 不同原因 → 指纹不同 → 不误累计（评审 MAJOR-3）；
    * 同一 PR 同一原因 → 指纹相同 → 正确累计；
    * 无身份且无原因信息 → 兜底 decision（缺乏结构化事实时的保守归并）。
    """
    decision = str(result.get('decision') or 'BLOCKED')
    payload = result.get('payload') or {}
    number = payload.get('number') if isinstance(payload, dict) else None
    reason = _reason_token(str(result.get('summary') or ''))
    if reason:
        return ('pr-%s:%s' % (number, reason)) if number else reason
    if number:
        # 只有身份、没有原因描述：至少区分任务，不与其他任务混为一谈。
        return 'pr-%s' % number
    return decision


def parse_result(raw: Optional[str]) -> Optional[dict]:
    """executions.result 是 JSON 文本；解析失败（脏数据）返回 None。"""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def consecutive(
    executions: List[dict],
    decision: str = 'BLOCKED',
    limit: int = ESCALATION_LIMIT,
) -> Tuple[int, Optional[str]]:
    """从最近一轮向前数连续同指纹 BLOCKED 次数。

    executions 须按时间升序（旧→新）。返回 (次数, 指纹)：次数达到 limit 时
    判定可升级；指纹为 None 表示连续段为空。任何非目标决策（RESOLVED/
    APPROVE 等反映环境已变化）的终结执行即打断计数；未终结（RUNNING/
    CANCELLED/FAILED）与脏数据行恒为中性——不计数也不打断（评审 MINOR）。
    """
    count = 0
    fingerprint_seen = None
    for execution in reversed(executions):
        result = parse_result(execution.get('result'))
        if result is None or execution.get('state') != 'SUCCEEDED':
            continue  # 未终结/失败/脏数据的中性行不断计数也不累计
        if result.get('decision') != decision:
            break  # 环境变化：非 BLOCKED 终结打断连续段
        fp = fingerprint(result)
        if fingerprint_seen is None:
            fingerprint_seen = fp
        elif fp != fingerprint_seen:
            break  # 原因变化：重计
        count += 1
        if count >= limit:
            break
    return count, fingerprint_seen


def cooldown_events(previous: Dict[str, dict], current: Dict[str, dict],
                    limit: int = ESCALATION_LIMIT) -> list:
    """冷却集合翻转事件（幂等留痕）：进入冷却/指纹变化/退出冷却。

    对比相邻两次 sync 看到的冷却状态，只对**状态变化**产出事件：
    * 新进入冷却（或指纹变化）→ ``handler.blocked.cooldown``（计数+原因）；
    * 冷却解除 → ``handler.blocked.recovered``。
    连续冷却的中间轮次不再重复写事件，避免 events 表无限增长（评审 MINOR）。
    previous/current 的 value 接受 ``{'fingerprint': ...}`` 或裸指纹字符串
    （service 持久化的 `blocked_cooldowns` meta 只留指纹节省空间）。
    """
    def fp(value):
        return value.get('fingerprint') if isinstance(value, dict) else value
    events = []
    for key, cd in current.items():
        before = previous.get(key)
        if before is None or fp(before) != fp(cd):
            events.append(('handler.blocked.cooldown',
                           {'key': key, 'count': cd.get('count'),
                            'fingerprint': fp(cd), 'limit': limit}))
    for key in previous:
        if key not in current:
            events.append(('handler.blocked.recovered', {'key': key}))
    return events


def probe_due(now: float, last_executed_at: Optional[float],
              interval: float) -> bool:
    """冷却 key 是否到了再一次探针（重新派生）的窗口。

    距离该 key 最近一次终结执行超过 ``interval`` 才返回 True：冷却触发后的
    头一个间隔内不重试（停止每轮烧会话），间隔过后派生一次探针项验证环境；
    环境已恢复则新执行非 BLOCKED → 除锁；仍阻塞则 BLOCKED 同因 → 越过阈值
    保持冷却，等下一个间隔再探。
    """
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval <= 0:
        raise ValueError('blocked probe interval must be a positive number')
    return (last_executed_at is None) or (now - last_executed_at) >= interval

def cooldown_keys(store, limit: int = ESCALATION_LIMIT) -> Dict[str, dict]:
    """达到连续 BLOCKED 阈值的 key 集合（评审 MAJOR-2：有界快照代替全表扫）。

    ``store.blocked_snapshot()`` 已按 key 截断最近 limit 行终结执行；此处只数
    连续同指纹段的长度与阈值比较 —— 判别逻辑与该存储快照的实现无关。
    """
    out: Dict[str, dict] = {}
    for key, rows in store.blocked_snapshot().items():
        count, fp = consecutive(rows)
        if count >= limit:
            out[key] = {'count': count, 'fingerprint': fp,
                        'last_created_at': rows[-1]['created_at']}
    return out
