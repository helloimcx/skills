#!/usr/bin/env python3
"""issue #280 接线测试：同因 BLOCKED 冷却 + 评审修复（repair:284）。

评审意见逐条落地验证：
- BLOCKER-1：冷却不再永久停派生——冷却中的 key 按探针节拍（blocked_probe_
  seconds）重新派生一条 probe 变体，环境恢复后连续计数归零、自动解除；
- MAJOR-2：blocked_snapshot 有界快照（kind/state/时间窗/每 key 最近 N 行，
  带 (item_id, created_at) 索引）；
- MAJOR-3：指纹含稳定原因（PR 前缀 + 原因 token），同 PR 不同原因不误冷却；
- MINOR：冷却翻转事件幂等（进入写一次、持续不刷、解除写一次）。

纯函数（指纹/连续计数/事件计算/探针节拍）覆盖在 test_blocked.py；本文件
验证 service.sync 写事务内的接线与端到端解除路径。
"""
import json
import time

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from _service_test_base import ServiceTestsBase


def _seed(service, key, decision='BLOCKED', summary='BLOCKED: http 502',
          count=1, state='SUCCEEDED', payload=None):
    """为某 work_item key 追加 count 条终结执行历史（时间升序）。"""
    db = service.store.db
    now = time.time()
    base = db.execute('SELECT COUNT(*) AS n FROM executions').fetchone()['n']
    for i in range(count):
        item_id = '%s-%d' % (key.replace(':', '-'), base + i)
        result = {'decision': decision, 'reviewer': 'verifier', 'summary': summary}
        if payload:
            result['payload'] = payload
        db.execute('INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?,?)',
                   (item_id, key, 'handler', 'v%d' % (base + i), '{}', '[]',
                    'SUCCEEDED', 10, now + i))
        db.execute('INSERT INTO executions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   ('e-%d' % (base + i), item_id, 'worker', base + i + 1,
                    now + 999 + i, state, None, None, None,
                    json.dumps(result, ensure_ascii=False),
                    'v%d' % (base + i), now + i, None))
    db.commit()


def _ready_keys(service):
    return [x['key'] for x in service.store.items('handler') if x['state'] == 'READY']


def _events(service, kind):
    return [r for r in service.store.db.execute(
        'SELECT kind FROM events WHERE kind=? ORDER BY id', (kind,))]


class BlockedCooldownDerivation(ServiceTestsBase):
    def test_three_same_blocked_stops_issue_derivation(self):
        s, p = self._fresh()
        p.data['issues'] = [self.issue()]
        s.sync()
        self.assertIn('issue:1', _ready_keys(s))  # 基线：正常派生
        _seed(s, 'issue:1', count=3)  # 之后三轮同因 BLOCKED
        s.sync()
        # 冷却静默期（默认探针间隔 6h）内不再派生普通项
        ready = [x for x in s.store.items('handler') if x['key'] == 'issue:1'
                 and x['state'] == 'READY']
        self.assertEqual(ready, [])

    def test_blocker1_probe_unlocks_cooldown(self):
        """评审 BLOCKER-1 e2e：冷却不再永久停派生——探针节拍到期后重新派生
        probe 变体（环境可再检验），环境恢复后连续计数归零 → 恢复正常派生。

        短探针间隔（1s）驱动：种子 3 条同因 BLOCKED → 进入冷却（静默期
        0 普通项）；把该 key 最近执行时间推旧（模拟冷却间隔已过）→ 再 sync
        派生 probe 变体；随后一条 RESOLVED 终结（环境恢复）→ 冷却解除。
        """
        s, p = self._fresh({'blocked_probe_seconds': 1})
        p.data['issues'] = [self.issue()]
        s.sync()
        _seed(s, 'issue:1', count=3)
        s.sync()
        self.assertEqual([x for x in s.store.items('handler')
                          if x['key'] == 'issue:1' and x['state'] == 'READY'], [])
        db = s.store.db
        db.execute("UPDATE executions SET created_at=created_at-10")
        db.commit()
        s.sync()
        probes = [x for x in s.store.items('handler') if x['key'] == 'issue:1'
                  and x['state'] == 'READY' and json.loads(x['payload']).get('probe')]
        self.assertEqual(len(probes), 1,
                         '冷却后探针节拍到期应重新派生 probe 变体（BLOCKER-1 解除路径）')
        # 环境恢复 → RESOLVED 终结打断连续计数 → 冷却解除 → 恢复正常派生
        _seed(s, 'issue:1', decision='RESOLVED', summary='环境恢复，占用释放')
        s.sync()
        self.assertIn('issue:1', _ready_keys(s))
        self.assertTrue(_events(s, 'handler.blocked.recovered'))

    def test_probe_keeps_cooldown_while_still_blocked(self):
        """评审 BLOCKER-1 节流语义：环境仍坏时探针派生一次 → handler 再报
        同因 BLOCKED → 冷却保持；随后再次 sync 不重复刷事件、不无限派生
        普通项（仍静默），只有又过一个探针间隔才重新派生。"""
        s, p = self._fresh({'blocked_probe_seconds': 1})
        p.data['issues'] = [self.issue()]
        s.sync()
        _seed(s, 'issue:1', count=3)
        s.sync()
        db = s.store.db
        db.execute("UPDATE executions SET created_at=created_at-10")
        db.commit()
        s.sync()
        probes = [x for x in s.store.items('handler') if x['key'] == 'issue:1'
                  and x['state'] == 'READY' and json.loads(x['payload']).get('probe')]
        self.assertEqual(len(probes), 1)
        # 环境仍坏：handler 领走探针后照样再报一条同因 BLOCKED（新终结执行
        # 把 last_created_at 拉回 now → 未到新间隔 → 不无限派生普通/探针项）
        _seed(s, 'issue:1', count=1)  # 与种子同 summary → 同指纹，继续累计
        s.sync()
        self.assertEqual(
            len([x for x in s.store.items('handler') if x['key'] == 'issue:1'
                 and x['state'] == 'READY']), 0)

    def test_cooldown_event_flip_idempotent(self):
        """评审 MINOR：cooldown 事件只在翻转时写——进入冷却 1 条、冷却持续不
        刷、解除写 recovered。"""
        s, p = self._fresh()
        _seed(s, 'issue:1', count=3)
        p.data['issues'] = [self.issue()]
        s.sync()
        self.assertEqual(len(_events(s, 'handler.blocked.cooldown')), 1)
        s.sync()  # 冷却持续 → 不再重复写
        self.assertEqual(len(_events(s, 'handler.blocked.cooldown')), 1)
        # 环境恢复（RESOLVED 终结）→ 连续计数归零 → 恢复派生 + recovered
        _seed(s, 'issue:1', decision='RESOLVED', summary='issue fixed')
        s.sync()
        self.assertIn('issue:1', _ready_keys(s))
        self.assertTrue(_events(s, 'handler.blocked.recovered'))

    def test_under_limit_still_derives(self):
        s, p = self._fresh()
        _seed(s, 'issue:1', count=2)
        p.data['issues'] = [self.issue()]
        s.sync()
        self.assertIn('issue:1', _ready_keys(s))

    def test_issue_resolved_resets(self):
        s, p = self._fresh()
        _seed(s, 'issue:1', count=2, summary='BLOCKED: 原因A')
        _seed(s, 'issue:1', decision='RESOLVED', summary='issue fixed')  # 打断
        p.data['issues'] = [self.issue()]
        s.sync()
        self.assertIn('issue:1', _ready_keys(s))

    def test_reason_change_resets(self):
        s, p = self._fresh()
        _seed(s, 'issue:1', count=2, summary='BLOCKED: 原因A')
        _seed(s, 'issue:1', count=1, summary='BLOCKED: 原因B')  # 指纹变了
        p.data['issues'] = [self.issue()]
        s.sync()
        self.assertIn('issue:1', _ready_keys(s))

    def test_running_rows_neutral(self):
        # 未终结的旧执行（RUNNING）不计入连续段，也不打断
        s, p = self._fresh()
        _seed(s, 'issue:1', count=2)
        _seed(s, 'issue:1', state='RUNNING')
        p.data['issues'] = [self.issue()]
        s.sync()
        self.assertIn('issue:1', _ready_keys(s))

    def test_repair_key_also_cooldowns(self):
        s, p = self._fresh()
        _seed(s, 'repair:99', count=3)
        pr = self.pr()
        pr['number'] = 99
        pr['mergeable'] = False
        pr['body'] = 'fix'
        p.data['issues'] = []
        p.data['prs'] = [pr]
        s.sync()
        self.assertNotIn('repair:99', _ready_keys(s))

    def test_major3_same_pr_different_reason_not_cooldown(self):
        """评审 MAJOR-3：同 PR 不同原因不再误判为同因累计冷却。"""
        s, p = self._fresh()
        _seed(s, 'repair:99', count=2, summary='BLOCKED: http 502',
              payload={'number': 99})
        _seed(s, 'repair:99', count=1, summary='BLOCKED: 原分支被外部 worktree 占用',
              payload={'number': 99})
        pr = self.pr()
        pr['number'] = 99
        pr['mergeable'] = False
        pr['body'] = 'fix'
        p.data['issues'] = []
        p.data['prs'] = [pr]
        s.sync()
        self.assertIn('repair:99', _ready_keys(s))  # 指纹不同 → 不冷却

    def test_snapshot_bounded(self):
        """评审 MAJOR-2：blocked_snapshot 只取最近窗口，旧执行不参与计数。"""
        s, p = self._fresh()
        # 60 天前的旧 BLOCKED 执行（超出默认 30 天窗口）
        db = s.store.db
        _seed(s, 'issue:1', count=3)
        db.execute("UPDATE executions SET created_at = created_at - 60*86400")
        db.commit()
        p.data['issues'] = [self.issue()]
        s.sync()
        # 窗口外的执行不进连续段 → 不冷却，正常派生
        self.assertIn('issue:1', _ready_keys(s))


if __name__ == '__main__':
    import unittest
    unittest.main()