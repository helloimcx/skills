#!/usr/bin/env python3
"""verify 证据落账与快照解耦的边界测试。

verify 的职责是产生证据（本地产、head 锚定、不过期）；输入 currency 由
消费点（report/publish）唯一负责。这里锁定三件事：快照陈旧不得让已完成的
门禁计算作废、输入换代同样照常落账且不取消执行、而 report 的 STALE_INPUT
守卫必须仍然在位——拦截只发生在该发生的地方。
"""
import json
import time

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from _service_test_base import git, ServiceTestsBase


class VerifyEvidenceTests(ServiceTestsBase):
    def _handler_round(self):
        wt, cfg = self._worktree(origin=True)
        s, p = self._fresh(cfg)
        pr = self.pr()
        pr['body'] = 'fix'  # 无 Closes 链接，不占 Issue 锁
        pr['head'] = {'sha': git(wt, 'rev-parse', 'HEAD'), 'ref': 'fix/mocx/bug'}
        p.data['issues'] = [self.issue()]
        p.data['prs'] = [pr]
        s.sync()
        item = [x for x in s.store.items('handler') if x['state'] == 'READY'][0]
        e = s.claim('handler', 'worker', item_id=item['id'])
        s.bind(e['id'], e['token'], wt, 'fix/mocx/bug')
        return s, p, e

    def test_verify_files_evidence_despite_stale_snapshot(self):
        s, p, e = self._handler_round()
        s.set_meta('synced_at', time.time() - 400)  # > snapshot_ttl 300s
        result = s.verify(e['id'], e['token'])
        self.assertTrue(result['passed'])
        row = s.store.db.execute('SELECT COUNT(*) c FROM evidence WHERE execution_id=?',
                                 (e['id'],)).fetchone()
        self.assertEqual(1, row['c'])
        state = s.store.db.execute('SELECT state FROM executions WHERE id=?',
                                   (e['id'],)).fetchone()['state']
        self.assertEqual('RUNNING', state)

    def test_verify_survives_input_version_churn(self):
        s, p, e = self._handler_round()
        p.data['base_sha'] = 'base2'  # 平台前进 → 下一轮 sync 换代重派生
        self.expire_observer()
        s.sync()
        result = s.verify(e['id'], e['token'])  # 旧协议在这里 STALE_INPUT + 取消
        self.assertTrue(result['passed'])
        state = s.store.db.execute('SELECT state FROM executions WHERE id=?',
                                   (e['id'],)).fetchone()['state']
        self.assertEqual('RUNNING', state)
        ev = json.loads(s.store.db.execute(
            'SELECT payload FROM evidence WHERE execution_id=?',
            (e['id'],)).fetchone()['payload'])
        self.assertEqual(e['item_version'], ev['input_version'])

    def test_report_still_guards_superseded_input(self):
        s, p, e = self._handler_round()
        p.data['base_sha'] = 'base2'
        self.expire_observer()
        s.sync()
        s.verify(e['id'], e['token'])  # 证据照常落账
        with self.assertRaisesRegex(ValueError, 'STALE_INPUT'):
            s.report(e['id'], e['token'], {'decision': 'APPROVE', 'reviewer': 'verifier',
                      'summary': 'x', 'action': 'edit_pr',
                      'payload': {'number': 2, 'title': 'fix', 'body': 'b'}})
        state = s.store.db.execute('SELECT state FROM executions WHERE id=?',
                                   (e['id'],)).fetchone()['state']
        self.assertEqual('CANCELLED', state)

    def test_superseded_green_verify_resolves_gate_signature(self):
        # 换代轮的绿跑现在可达签名自愈分支（旧协议在 verify 门口取消、不可达）：
        # 同 item 历史失败的 gate 签名被绿跑 resolve——gate 健康与输入 currency 无关。
        s, p, e = self._handler_round()
        db = s.store.db
        now = time.time()
        db.execute('INSERT INTO executions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   ('e-prior', e['item_id'], 'w', 987654321, now + 99, 'FAILED',
                    None, None, None, None, 'v0', now, None))
        db.execute('INSERT INTO evidence VALUES (?,?,?,?,?)',
                   ('e-prior-v', 'e-prior', 'verification',
                    json.dumps({'passed': False, 'env_signature': 'sig-1'}), now))
        s.set_meta('gates', {'sig-1': {'state': 'open'}})
        p.data['base_sha'] = 'base2'
        self.expire_observer()
        s.sync()
        result = s.verify(e['id'], e['token'])
        self.assertTrue(result['passed'])
        self.assertEqual('repaired', s.meta('gates')['sig-1']['state'])
