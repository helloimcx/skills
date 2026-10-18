"""blocked 模块测试：同因 BLOCKED 指纹提取与连续计数（issue #280）。

覆盖：稳定原因码优先（显式 reason= / 括号细节剥离）、PR 号只作前缀维度
（评审 MAJOR-3：同 PR 不同原因不再误累计）、连续计数（同因累计 / 异因打断
/ 非 BLOCKED 终结打断 / RUNNING/CANCELLED/FAILED 中性行不断计数）、冷却
翻转事件幂等（评审 MINOR）、探针节拍判据（评审 BLOCKER-1 的解除机制一半）。
"""
import json
import time
import unittest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codeloop.cp import blocked


def _execution(decision, summary='', action=None, payload=None, state='SUCCEEDED',
               result=None):
    if result is None:
        result = {'decision': decision, 'summary': summary, 'reviewer': 'x'}
        if action:
            result['action'] = action
        if payload:
            result['payload'] = payload
    return {'state': state, 'result': json.dumps(result, ensure_ascii=False)}


class FingerprintTests(unittest.TestCase):
    def test_pr_number_with_explicit_reason_code(self):
        # 同 PR 不同原因 → 指纹不同（评审 MAJOR-3：不再退化为 pr-<n>）
        a = {'decision': 'BLOCKED', 'summary': 'BLOCKED: http 502',
             'payload': {'number': 617}}
        b = {'decision': 'BLOCKED', 'summary': 'BLOCKED: 原分支被外部 worktree 占用',
             'payload': {'number': 617}}
        self.assertNotEqual(blocked.fingerprint(a), blocked.fingerprint(b))
        self.assertTrue(blocked.fingerprint(a).startswith('pr-617:'))

    def test_explicit_reason_token_wins_over_text(self):
        # handler 显式原因码（reason= / 原因=）→ 行为稳，措辞漂移不影响归因
        result = {'decision': 'BLOCKED',
                  'summary': 'BLOCKED: worktree 占用\nreason=worktree-occupied'}
        self.assertEqual(blocked.fingerprint(result), 'code:worktree-occupied')

    def test_bracket_details_stripped(self):
        # 括号内路径/编号等细碎事实剥离 → 多轮措辞漂移不断链（评审 MAJOR-3）
        a = {'decision': 'BLOCKED',
             'summary': 'BLOCKED: 原分支被外部 worktree 占用（.git-worktrees/report-perf-615）'}
        b = {'decision': 'BLOCKED',
             'summary': 'BLOCKED: 原分支被外部 worktree 占用（.git-worktrees/other-99）'}
        self.assertEqual(blocked.fingerprint(a), blocked.fingerprint(b))
        self.assertNotIn('git-worktrees', blocked.fingerprint(a))

    def test_no_structure_falls_back_to_decision(self):
        result = {'decision': 'BLOCKED', 'summary': '随便写的原因'}
        self.assertEqual(blocked.fingerprint(result), 'BLOCKED')

    def test_issue_key_no_pr_number(self):
        # issue 任务（无 payload.number）→ 指纹即原因段；无 PR 上下文不混淆
        result = {'decision': 'BLOCKED', 'summary': 'BLOCKED: 测试环境不可用'}
        self.assertEqual(blocked.fingerprint(result), '测试环境不可用')


class ConsecutiveTests(unittest.TestCase):
    def test_three_same_reason_escalates(self):
        rows = [
            _execution('BLOCKED', 'BLOCKED: 外部 worktree 占用'),
            _execution('BLOCKED', 'BLOCKED: 外部 worktree 占用'),
            _execution('BLOCKED', 'BLOCKED: 外部 worktree 占用'),
        ]
        count, fp = blocked.consecutive(rows)
        self.assertEqual(count, 3)
        self.assertEqual(fp, '外部 worktree 占用')

    def test_reason_change_breaks_streak(self):
        rows = [
            _execution('BLOCKED', 'BLOCKED: 外部 worktree 占用'),
            _execution('BLOCKED', 'BLOCKED: base 门禁红'),
        ]
        count, fp = blocked.consecutive(rows)
        self.assertEqual(count, 1)  # 只数到最近一段
        self.assertEqual(fp, 'base 门禁红')

    def test_non_blocked_terminus_resets_streak(self):
        rows = [
            _execution('BLOCKED', 'BLOCKED: 外部 worktree 占用'),
            _execution('RESOLVED', '环境已恢复'),
        ]
        count, fp = blocked.consecutive(rows)
        self.assertEqual(count, 0)
        self.assertIsNone(fp)

    def test_unfinalized_rows_are_neutral(self):
        for state in ('RUNNING', 'CANCELLED', 'FAILED'):
            rows = [
                _execution('BLOCKED', 'BLOCKED: worktree 占用'),
                _execution('BLOCKED', 'BLOCKED: worktree 占用', state=state),
            ]
            count, fp = blocked.consecutive(rows)
            self.assertEqual(count, 1, state)  # 未终结/失败行不断计数也不累计

    def test_empty_history(self):
        count, fp = blocked.consecutive([])
        self.assertEqual(count, 0)
        self.assertIsNone(fp)

    def test_parse_result_tolerates_garbage(self):
        self.assertIsNone(blocked.parse_result(None))
        self.assertIsNone(blocked.parse_result('not-json'))
        self.assertEqual(blocked.parse_result('{"decision": "BLOCKED"}'),
                         {'decision': 'BLOCKED'})


class CooldownEventTests(unittest.TestCase):
    def test_entry_and_recovery_events(self):
        prev = {}
        cur = {'issue:1': {'count': 3, 'fingerprint': 'worktree 占用'}}
        events = blocked.cooldown_events(prev, cur)
        self.assertEqual([e[0] for e in events],
                         ['handler.blocked.cooldown'])
        # 下一轮仍冷却 → 事件不变（评审 MINOR：持续期不再刷事件）
        events = blocked.cooldown_events(cur, dict(cur))
        self.assertEqual(events, [])
        # 冷却解除 → 恢复事件
        events = blocked.cooldown_events(dict(cur), {})
        self.assertEqual([e[0] for e in events],
                         ['handler.blocked.recovered'])

    def test_fingerprint_change_re_announces(self):
        prev = {'issue:1': {'count': 3, 'fingerprint': 'A'}}
        cur = {'issue:1': {'count': 4, 'fingerprint': 'B'}}
        events = blocked.cooldown_events(prev, cur)
        self.assertEqual([e[0] for e in events],
                         ['handler.blocked.cooldown'])
        self.assertEqual(events[0][1]['fingerprint'], 'B')


class ProbeTests(unittest.TestCase):
    def test_probe_due_by_interval(self):
        now = time.time()
        self.assertTrue(blocked.probe_due(now, None, 60))       # 无历史 → 到期
        self.assertFalse(blocked.probe_due(now, now, 60))     # 刚执行过 → 未到
        self.assertTrue(blocked.probe_due(now, now - 120, 60))

    def test_probe_interval_validation(self):
        with self.assertRaises(ValueError):
            blocked.probe_due(time.time(), None, 0)
        with self.assertRaises(ValueError):
            blocked.probe_due(time.time(), None, -1)


if __name__ == '__main__':
    unittest.main()
