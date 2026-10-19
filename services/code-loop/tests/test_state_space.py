# -*- coding: utf-8 -*-
"""CP 状态空间枚举器测试。

三层：
1. 引擎契约（玩具模型钉死：适用面裁剪、first-match、遮蔽、格子键、台账、check 聚合）
2. 规则表回归（已知事故 #235/#239/STALE_INPUT 的格子裁决）
3. 一致性门禁（代码理由码 ↔ 表双向对齐；无码守卫须裁决；完备性）
"""
import json
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codeloop.cp import state_space as ss
from codeloop.cp import state_rules as sr


def _toy_model():
    dims = {'s': ('A', 'B'), 't': ('X', 'Y')}
    actions = {'act1': ('s', 't'), 'act2': ('s',)}
    prunings = [(lambda c: c.get('s') == 'A' and c.get('t') == 'Y',
                 'A×Y 组合结构不可达（示例剪枝不变量）')]
    rules = [
        ss.Rule('r1', 'act1', lambda c: c['s'] == 'A', 'ALLOW', None, 'A 放行'),
        ss.Rule('r2', 'act1', lambda c: c['s'] == 'B' and c['t'] == 'X',
                'REJECT', 'B_FORBIDDEN', 'B×X 拒绝（B×Y 留作未定义）'),
        ss.Rule('r3', 'act2', lambda c: True, 'ALLOW', None, 'act2 恒放行',
                fallback=True),
    ]
    return ss.Model(dimensions=dims, actions=actions, prunings=prunings, rules=rules)


class TestEngineContracts(unittest.TestCase):
    def test_contexts_respect_pruning(self):
        model = _toy_model()
        keys = [ss.cell_key('act1', c) for c in ss.contexts_for(model, 'act1')]
        self.assertEqual(sorted(keys), sorted([
            'act1|s=A,t=X', 'act1|s=B,t=X', 'act1|s=B,t=Y']))

    def test_cell_key_is_deterministic(self):
        key = ss.cell_key('act1', {'t': 'X', 's': 'A'})
        self.assertEqual(key, 'act1|s=A,t=X')

    def test_first_match_wins(self):
        model = _toy_model()
        decision = ss.evaluate(model, 'act1', {'s': 'B', 't': 'X'})
        self.assertEqual(decision.rule.id, 'r2')
        self.assertEqual(decision.decision, 'REJECT')
        self.assertEqual(decision.reason, 'B_FORBIDDEN')

    def test_undefined_when_no_rule_matches(self):
        model = _toy_model()
        decision = ss.evaluate(model, 'act1', {'s': 'B', 't': 'Y'})
        self.assertEqual(decision.decision, 'UNDEFINED')
        self.assertIsNone(decision.rule)

    def test_shadow_detected(self):
        model = _toy_model()
        model.rules.append(ss.Rule('r1b', 'act1', lambda c: c['s'] == 'A',
                                   'REJECT', 'CONFLICT', '与 r1 冲突'))
        result = ss.evaluate(model, 'act1', {'s': 'A', 't': 'X'})
        self.assertEqual(result.decision, 'ALLOW')
        self.assertEqual([r.id for r in result.shadows], ['r1b'])

    def test_fallback_rule_never_shadows(self):
        model = _toy_model()
        model.rules.append(ss.Rule('r5', 'act1', lambda c: True, 'DERIVE',
                                   None, '兜底', fallback=True))
        result = ss.evaluate(model, 'act1', {'s': 'A', 't': 'X'})
        self.assertEqual(result.shadows, [])

    def test_fallback_must_be_last(self):
        model = _toy_model()
        model.rules.insert(1, ss.Rule('r6', 'act1', lambda c: True, 'ALLOW',
                                      None, '前置兜底', fallback=True))
        with self.assertRaises(ValueError):
            ss.Model(dimensions=model.dimensions, actions=model.actions,
                     prunings=model.prunings, rules=model.rules)

    def test_classify_reports_buckets(self):
        model = _toy_model()
        result = ss.classify(model, review={})
        by_decision = {}
        for cell in result.cells:
            by_decision[cell.decision] = by_decision.get(cell.decision, 0) + 1
        # act1: A,X=ALLOW  B,X=REJECT  B,Y=UNDEFINED；act2: A,B=ALLOW
        self.assertEqual(by_decision, {'ALLOW': 3, 'REJECT': 1, 'UNDEFINED': 1})
        self.assertEqual(len(result.undefined), 1)
        self.assertEqual(result.undefined[0].key, 'act1|s=B,t=Y')

    def test_disposition_clears_undefined_failure(self):
        model = _toy_model()
        key = 'act1|s=B,t=Y'
        result = ss.classify(model, review={key: {'status': 'accepted', 'note': '示例裁决'}})
        self.assertEqual(result.failures, [])
        self.assertTrue(result.complete)

    def test_invalid_disposition_status_rejected(self):
        model = _toy_model()
        with self.assertRaises(ValueError):
            ss.classify(model, review={'act1|s=B,t=Y': {'status': 'whatever', 'note': ''}})


class TestScan(unittest.TestCase):
    def _write(self, tmp, text):
        path = tmp / 'fake_cp.py'
        path.write_text(textwrap.dedent(text), encoding='utf-8')
        return [path]

    def test_scan_extracts_coded_and_uncoded_raises(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            paths = self._write(Path(td), '''\
                raise ValueError('STALE_INPUT: source changed')
                raise ValueError('INVALID_SNAPSHOT')
                raise ValueError('execution is missing, fenced, or terminal')
                raise ValueError('ttl must be a finite positive number')
            ''')
            scan = ss.scan_code_reason_codes(paths)
            self.assertIn('STALE_INPUT', scan.codes)
            self.assertIn('INVALID_SNAPSHOT', scan.codes)
            coded = {msg for _, _, msg in scan.uncoded}
            self.assertIn('execution is missing, fenced, or terminal', coded)
            self.assertIn('ttl must be a finite positive number', coded)


class TestConsistencyGate(unittest.TestCase):
    """规则表 ↔ 代码 双向对齐（门禁核心）。"""

    def setUp(self):
        self.model = sr.build()
        self.review = sr.load_review()
        self.scan = sr.scan_cp_sources()

    def test_every_scanned_code_is_documented(self):
        covered = set(sr.DOCUMENTED_CODES) | set(sr.INPUT_VALIDATION_EXEMPT) | set(sr.NON_REASON_LITERALS)
        for rule in self.model.rules:
            if rule.reason:
                covered.add(rule.reason)
        missing = set(self.scan.codes) - covered
        self.assertEqual(missing, set(),
                         '代码中存在未入表/未豁免的理由码: %s' % sorted(missing))

    def test_every_rule_reason_exists_in_code(self):
        scan_codes = set(self.scan.codes)
        for rule in self.model.rules:
            if not rule.reason or rule.reason == sr.UNCODED_GUARD:
                continue
            self.assertIn(rule.reason, scan_codes,
                          '规则 %s 引用的理由码代码中不存在' % rule.id)
        for code in sr.DOCUMENTED_CODES:
            self.assertIn(code, scan_codes, '文档化代码 %s 未在代码中发现' % code)

    def test_uncoded_raises_all_dispositioned(self):
        undispositioned = [msg for _, _, msg in self.scan.uncoded
                           if sr.disposition_for(msg) is None]
        self.assertEqual(undispositioned, [],
                         '存在未裁决的无码 raise: %s' % undispositioned)
        # guard 类裁决 = 状态守卫缺理由码，是枚举器要暴露的发现
        guards = [msg for _, _, msg in self.scan.uncoded
                  if str(sr.disposition_for(msg)).startswith('guard')]
        self.assertIn('execution is missing, fenced, or terminal', guards)

    def test_model_is_complete(self):
        result = ss.classify(self.model, review=self.review)
        self.assertEqual(result.failures, [],
                         '枚举未完备: %s' % result.failures[:5])


class TestKnownIncidentRegressions(unittest.TestCase):
    """已知事故必须在表里得到正确裁决——表 ≙ 代码真实语义的证明。"""

    @classmethod
    def setUpClass(cls):
        cls.model = sr.build()

    def _eval(self, action, **ctx):
        return ss.evaluate(self.model, action, ctx)

    def test_issue235_late_failed_ack_vs_terminal_execution(self):
        """#235：SPAWNED 指令 × execution 已终态 × 迟到 FAILED ack ⇒ CAS 失败（放行 null），
        绝不覆盖已完成轮次。"""
        d = self._eval('command_ack_failed', command_state='SPAWNED',
                       command_exec='LINKED_TERMINAL')
        self.assertEqual(d.decision, 'ALLOW')
        self.assertIn('CAS', d.invariant)

    def test_issue235_sibling_spawned_still_open(self):
        d = self._eval('command_ack_failed', command_state='SPAWNED',
                       command_exec='LINKED_OPEN')
        self.assertEqual(d.decision, 'ALLOW')
        self.assertIn('FAILED', d.invariant)

    def test_stale_input_clean_outbox_cancels(self):
        """STALE_INPUT 抖动：SUPERSEDED × 干净 outbox ⇒ 拒绝并推导 CANCELLED，重领新版。"""
        d = self._eval('report', role='reviewer', exec_state='RUNNING', lease='VALID',
                       token='MATCH', currency='SUPERSEDED', outbox='NONE',
                       result='PRESENT', bound='BOUND')
        self.assertEqual(d.decision, 'REJECT')
        self.assertEqual(d.reason, 'STALE_INPUT')
        self.assertIn('CANCELLED', d.invariant)

    def test_stale_input_unresolved_publication_wins(self):
        """红线优先于换代：未决发布不被换代取消。"""
        d = self._eval('report', role='reviewer', exec_state='RUNNING', lease='VALID',
                       token='MATCH', currency='SUPERSEDED', outbox='UNKNOWN',
                       result='PRESENT', bound='BOUND')
        self.assertEqual(d.reason, 'STALE_INPUT')
        self.assertIn('reconcile', d.invariant)

    def test_issue239_terminal_item_new_version_revives(self):
        """#239：换代后同 key 新版本 ⇒ put_item 造新 READY 行（终态行复活）。"""
        d = self._eval('put_item', item_state='SUCCEEDED', currency='SUPERSEDED')
        self.assertEqual(d.decision, 'ALLOW')
        self.assertIn('复活', d.invariant)

    def test_expired_lease_does_not_close_write_channel(self):
        """store.py 头注不变量：过期只是回收资格；未被 steal 前 holder 的写与在途无异。"""
        d = self._eval('verify', role='handler', exec_state='RUNNING', lease='EXPIRED',
                       token='MATCH', bound='BOUND')
        self.assertEqual(d.decision, 'ALLOW')

    def test_verify_is_currency_blind(self):
        """PR#245：verify 不消费输入 currency（证据本地钉扎，消费点执法在 report/publish）。"""
        d = self._eval('verify', role='handler', exec_state='RUNNING', lease='VALID',
                       token='MATCH', bound='BOUND')
        self.assertEqual(d.decision, 'ALLOW')
        self.assertIn('currency', d.invariant)

    def test_unresolved_publication_never_auto_terminalized(self):
        """红线：SENDING/UNKNOWN 不被 sweep/steal 自动终态化。"""
        d = self._eval('sweep_recover', exec_state='RUNNING', lease='EXPIRED',
                       outbox='SENDING', result='PRESENT')
        self.assertEqual(d.decision, 'ALLOW')
        self.assertIn('人工', d.invariant)

    def test_fence_rejects_without_clock(self):
        """fencing 只查 token+终态：token 不符 ⇒ 拒绝，与租约时钟无关。"""
        d = self._eval('report', role='handler', exec_state='RUNNING', lease='VALID',
                       token='MISMATCH', currency='CURRENT', outbox='NONE',
                       result='ABSENT', bound='BOUND')
        self.assertEqual(d.decision, 'REJECT')
        self.assertEqual(d.reason, sr.UNCODED_GUARD)


class TestDirectCodeLocks(unittest.TestCase):
    """直测锁定：事故回归不只锁表裁决，直接锁 store/command_store 的代码行为——
    表与代码同时被钉住，一边漂移测试即红（Review M4）。"""

    def setUp(self):
        import tempfile
        from codeloop.cp.store import Store
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_issue235_ack_failed_vs_terminal_execution_is_cas_loss(self):
        item = self.store.put_item('review:1', 'reviewer', 'v1',
                                   {'pr': 1}, ['pr-1'])
        row, _ = self.store.enqueue_command('review_round', 1)
        claimed = self.store.mark_dispatched(row['id'], 'm1')
        self.assertIsNotNone(claimed)
        acked = self.store.ack_command(row['id'], 'm1', 'spawned', execution='e1')
        self.assertEqual(acked['state'], 'SPAWNED')
        # 执行已终态（此处直接落一行终态执行）：迟到的 FAILED ack 必须 CAS 失败
        with self.store.transaction():
            self.store.db.execute(
                "INSERT INTO executions (id,item_id,worker,token,expires_at,"
                "state,item_version,created_at) VALUES (?,?,?,?,?,'SUCCEEDED',?,?)",
                ('e1', item['id'], 'w', 1, 0, 'v1', 0))
        self.assertIsNone(self.store.ack_command(row['id'], 'm1', 'failed'))
        fresh = dict(self.store.db.execute(
            'SELECT state FROM commands WHERE id=?', (row['id'],)).fetchone())
        self.assertEqual(fresh['state'], 'SPAWNED')  # 未被覆盖

    def test_issue239_same_version_cancelled_row_revives_to_ready(self):
        first = self.store.put_item('issue:1', 'handler', 'v1', {}, ['issue-1'])
        # 同 key 新版本入队后旧 READY 行被 CANCELLED（一代一活口）
        self.store.put_item('issue:1', 'handler', 'v2', {}, ['issue-1'])
        old = dict(self.store.db.execute(
            'SELECT state FROM work_items WHERE id=?', (first['id'],)).fetchone())
        self.assertEqual(old['state'], 'CANCELLED')
        # 同 (key,version=v1) 再落 ⇒ CANCELLED 行复活为 READY（#239）
        revived = self.store.put_item('issue:1', 'handler', 'v1', {}, ['issue-1'])
        self.assertEqual(revived['id'], first['id'])
        self.assertEqual(revived['state'], 'READY')

    def test_stale_input_cancel_requires_clean_outbox(self):
        item = self.store.put_item('issue:2', 'handler', 'v1', {}, ['issue-2'])
        row = self.store.claim('handler', 'm/1', item_id=item['id'])
        self.assertIsNotNone(row)
        # 干净 outbox：换代取消成立，项行随执行 CANCELLED
        e = dict(self.store.db.execute(
            'SELECT * FROM executions WHERE id=?', (row['id'],)).fetchone())
        fresh_item = dict(self.store.db.execute(
            'SELECT * FROM work_items WHERE id=?', (item['id'],)).fetchone())
        self.store.cancel_stale(e, fresh_item)
        gone = self.store.db.execute(
            'SELECT state FROM executions WHERE id=?', (row['id'],)).fetchone()
        self.assertEqual(gone['state'], 'CANCELLED')
        # 未决发布：取消被拒（STALE_INPUT: unresolved publication）
        item2 = self.store.put_item('issue:3', 'handler', 'v1', {}, ['issue-3'])
        row2 = self.store.claim('handler', 'm/1', item_id=item2['id'])
        self.store.db.execute('INSERT INTO outbox VALUES (?,?,?,?,?,NULL)',
                              ('op1', row2['id'], 'comment_issue', '{}', 'SENDING'))
        e2 = dict(self.store.db.execute(
            'SELECT * FROM executions WHERE id=?', (row2['id'],)).fetchone())
        it2 = dict(self.store.db.execute(
            'SELECT * FROM work_items WHERE id=?', (item2['id'],)).fetchone())
        with self.assertRaises(ValueError) as ctx:
            self.store.cancel_stale(e2, it2)
        self.assertIn('STALE_INPUT', str(ctx.exception))
        alive = self.store.db.execute(
            'SELECT state FROM executions WHERE id=?', (row2['id'],)).fetchone()
        self.assertEqual(alive['state'], 'RUNNING')  # limbo 不被取消


class TestDeterminism(unittest.TestCase):
    def test_report_is_byte_stable(self):
        import re
        model = sr.build()
        review = sr.load_review()
        first = ss.render_report(ss.classify(model, review=review))
        second = ss.render_report(ss.classify(model, review=review))
        self.assertEqual(first, second)
        self.assertIsNone(re.search(r'\d{4}-\d{2}-\d{2}', first),
                          '报告渗漏了日期/时间戳')


if __name__ == '__main__':
    unittest.main()
