#!/usr/bin/env python3
import copy
import json
import os
import socket
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts"),
           str(_Path(__file__).resolve().parent)):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.cp.runtime import EXAM_PATH
from codeloop.cp.exam import GUARD_NOTE as EXAM_GUARD_NOTE
from codeloop.cp.service import Service
from _service_test_base import Platform, git, ServiceTestsBase

class ServiceGateTests(ServiceTestsBase):
    def test_self_review_and_unknown_report_fields_rejected(self):
        self.s.sync()
        e = self.s.claim('discover', 'inspector')
        for result in [{'decision': 'CONFIRMED', 'reviewer': 'inspector', 'summary': 'proof'},
                       {'decision': 'CONFIRMED', 'reviewer': 'other', 'summary': 'proof', 'tests_pass': True}]:
            with self.assertRaises(ValueError):
                self.s.report(e['id'], e['token'], result)

    def test_reviewer_approved_requires_coverage(self):
        # helper 内部首次 report 即因缺 coverage 被拒（COVERAGE_REQUIRED）
        with self.assertRaisesRegex(ValueError, 'COVERAGE_REQUIRED'):
            self._approved_reviewer(coverage=None)

    def test_reviewer_approved_coverage_ok_passes(self):
        s, p, e = self._approved_reviewer(coverage=self.GOOD_COVERAGE)
        # report 已通过；再 publish 校验覆盖度行确定性渲染
        s.publish(e['id'], e['token'])
        body = p.writes[-1][1]['body']
        self.assertIn('Coverage: 2/4 reviewed (50%), 2 skipped', body)

    def test_reviewer_approved_coverage_empty_diff_passes(self):
        """空 diff：total=0 → rate=1.0，覆盖度核算合法通过。"""
        s, p, e = self._approved_reviewer(
            coverage={'total': 0, 'reviewed': [], 'skipped': [],
                      'ok': True, 'rate': 1.0})
        s.publish(e['id'], e['token'])
        self.assertIn('Coverage: 0/0 reviewed (100%), 0 skipped',
                      p.writes[-1][1]['body'])

    def test_reviewer_approved_coverage_zero_rate_passes(self):
        """全跳过核算合法：reviewed 空、rate=0.0，只要每条跳过带理由。"""
        s, p, e = self._approved_reviewer(
            coverage={'total': 2, 'reviewed': [],
                      'skipped': [{'path': 'a.lock', 'reason': 'lockfile'},
                                  {'path': 'b.min.js', 'reason': 'generated'}],
                      'ok': True, 'rate': 0.0})
        s.publish(e['id'], e['token'])
        self.assertIn('Coverage: 0/2 reviewed (0%), 2 skipped',
                      p.writes[-1][1]['body'])

    def test_reviewer_approved_coverage_incomplete_rejected(self):
        bad = dict(self.GOOD_COVERAGE, ok=False)
        with self.assertRaisesRegex(ValueError, 'COVERAGE_INCOMPLETE'):
            self._approved_reviewer(coverage=bad)

    def test_reviewer_approved_coverage_invalid_rejected(self):
        # 非 dict → INVALID_COVERAGE（而非 AttributeError）
        with self.assertRaisesRegex(ValueError, 'INVALID_COVERAGE'):
            self._approved_reviewer(coverage='oops')
        # 结构坏（reviewed 非列表）
        with self.assertRaisesRegex(ValueError, 'INVALID_COVERAGE'):
            self._approved_reviewer(coverage=dict(self.GOOD_COVERAGE, reviewed='app.py'))
        # 结构坏（bool 冒充 int/float）
        with self.assertRaisesRegex(ValueError, 'INVALID_COVERAGE'):
            self._approved_reviewer(coverage=dict(self.GOOD_COVERAGE, total=True))
        with self.assertRaisesRegex(ValueError, 'INVALID_COVERAGE'):
            self._approved_reviewer(coverage=dict(self.GOOD_COVERAGE, rate=True))
        # 一致性：|reviewed|+|skipped| != total
        with self.assertRaisesRegex(ValueError, 'INVALID_COVERAGE'):
            self._approved_reviewer(
                coverage=dict(self.GOOD_COVERAGE, reviewed=['app.py']))
        # 一致性：reviewed/skipped 重叠
        with self.assertRaisesRegex(ValueError, 'INVALID_COVERAGE'):
            self._approved_reviewer(
                coverage=dict(self.GOOD_COVERAGE,
                              reviewed=['app.py', 'package-lock.json']))
        # 派生值：rate 与 count 不符
        with self.assertRaisesRegex(ValueError, 'INVALID_COVERAGE'):
            self._approved_reviewer(coverage=dict(self.GOOD_COVERAGE, rate=0.9))

    def test_non_approved_reviewer_coverage_optional(self):
        """非 Approved 判词 coverage 可选；提供时仍过一致性校验，ok 可 false。"""
        s, p = self._fresh({})
        pr = self.pr()
        pr['body'] = 'fix'
        p.data['issues'] = []
        p.data['prs'] = [pr]
        s.sync()
        item = [x for x in s.store.items('reviewer') if x['state'] == 'READY'][0]
        e = s.claim('reviewer', 'worker', item_id=item['id'])
        # 不带 coverage 的非 Approved 判词照常通过
        s.report(e['id'], e['token'], {'decision': 'Changes requested',
                  'reviewer': 'verifier', 'summary': 'blocking',
                  'action': 'review_comment', 'payload': {'number': 2, 'body': 'x'}})
        # 带 coverage（ok=false）同样通过
        s, p = self._fresh({})
        p.data['issues'] = []
        p.data['prs'] = [pr]
        s.sync()
        item = [x for x in s.store.items('reviewer') if x['state'] == 'READY'][0]
        e = s.claim('reviewer', 'worker', item_id=item['id'])
        s.report(e['id'], e['token'], {'decision': 'Changes requested',
                  'reviewer': 'verifier', 'summary': 'blocking',
                  'action': 'review_comment', 'payload': {'number': 2, 'body': 'x'},
                  'coverage': dict(self.GOOD_COVERAGE, ok=False)})

    def test_handler_report_with_coverage_allowed(self):
        """coverage 字段对 handler 同样合法（可选，不做 Approved 强制）。"""
        wt, cfg = self._worktree(origin=True)
        s, p = self._fresh(cfg)
        pr = self.pr()
        pr['reviews'] = [{'user': {'id': 9}, 'state': 'REQUEST_CHANGES', 'body': 'fix it'}]
        p.data['issues'] = [self.issue()]
        p.data['prs'] = [pr]
        s.sync()
        item = [x for x in s.store.items('handler') if x['state'] == 'READY'][0]
        e = s.claim('handler', 'worker', item_id=item['id'])
        s.bind(e['id'], e['token'], wt, 'fix/mocx/bug')
        s.verify(e['id'], e['token'])
        s.report(e['id'], e['token'], {'decision': 'APPROVE', 'reviewer': 'verifier',
                  'summary': 'fixed', 'action': 'edit_pr',
                  'payload': {'number': 2, 'title': 'fix', 'body': 'fixed per review'},
                  'coverage': self.GOOD_COVERAGE})

    def test_discover_cannot_comment_pr(self):
        self.platform.data['prs'] = [self.pr()]
        self.s.sync()
        e = self.s.claim('discover', 'inspector')
        with self.assertRaises(ValueError):
            self.s.report(e['id'], e['token'], {'decision': 'CONFIRMED', 'reviewer': 'other',
                          'summary': 'evidence', 'action': 'comment_issue',
                          'payload': {'number': 2, 'body': 'bad'}})

    def test_policy_includes_interpreter_only_when_pinned(self):
        self.assertNotIn('interpreter', self.s.policy())
        self.cfg['interpreter'] = sys.executable
        self.assertEqual(self.s.policy()['interpreter'], sys.executable)

    def test_interpreter_change_invalidates_item_versions(self):
        self.s.sync()
        before = [x for x in self.s.store.items('discover') if x['state'] == 'READY'][0]['version']
        self.cfg['interpreter'] = sys.executable
        self.s.sync()
        after = [x for x in self.s.store.items('discover') if x['state'] == 'READY'][0]['version']
        self.assertNotEqual(before, after)

    def test_derive_all_open_issues_without_prefix_admission(self):
        # The static prefix admission is gone: every open issue derives; the
        # needs-human label and [Gap] title stay the deterministic exclusions.
        self.platform.data['issues'] = [
            self.issue(),
            dict(self.issue(), number=7, title='random question'),
            dict(self.issue(), number=8, title='[Gap] product'),
            dict(self.issue(), number=9, title='[BUG] reported', labels=[{'name': 'needs-human'}])]
        self.s.sync()
        keys = {x['key'] for x in self.s.store.items('handler') if x['state'] == 'READY'}
        self.assertEqual(keys, {'issue:1', 'issue:7'})
        self.assertNotIn('issue_prefixes', self.s.policy())

    def test_derive_excludes_needs_human_title_prefix(self):
        """打标机制是标题前缀（gitea-ops `issue tag`），故 derive 排除必须认前缀。

        回归：v5 全派生上线后 derive 只认 needs-human label 与 `[Gap]` 前缀，
        而打标把标题改成 `[❓needs-human] [Gap] ...` —— `[Gap]` 前缀判定随之失效，
        已打标的决策类 issue 被重新派生为 READY 工作项（2026-09-13 实测 #7/#17/#18/#109）。
        """
        self.platform.data['issues'] = [
            self.issue(),
            dict(self.issue(), number=7, title='[❓needs-human] [Gap] product'),
            dict(self.issue(), number=8, title='[❓needs-human] 选型'),
            dict(self.issue(), number=9, title='[❓needs-human] [Drift] x')]
        self.s.sync()
        keys = {x['key'] for x in self.s.store.items('handler') if x['state'] == 'READY'}
        self.assertEqual(keys, {'issue:1'})

    # ---- baseline-failure derivation: verify evidence feeds the discover item ----

    def test_baseline_failures_derive_into_discover_payload(self):
        failed = {'failure_class': 'code', 'exit_code': 1,
                  'failed_nodes': ['tests/a.py::t1'],
                  'failed_line': 'FAILED tests/a.py::t1 - NameError: x'}
        self._seed_verification('x1', 'review:301', 'h1', [dict(failed)])
        self._seed_verification('x2', 'review:300', 'h2', [dict(failed)])
        self.s.sync()
        candidates = self._discover_payload()['baseline_failures']
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['node'], 'tests/a.py::t1')
        self.assertEqual(candidates[0]['heads'], 2)
        self.assertEqual(candidates[0]['sources'], ['review:300', 'review:301'])
        self.assertEqual(candidates[0]['sample_line'], 'FAILED tests/a.py::t1 - NameError: x')

    def test_discover_payload_has_no_baseline_key_without_candidates(self):
        self.s.sync()
        self.assertNotIn('baseline_failures', self._discover_payload())

    def test_baseline_candidates_exclude_env_expired_and_passed(self):
        node = {'failure_class': 'code', 'exit_code': 1, 'failed_nodes': ['tests/a.py::t1'],
                'failed_line': 'FAILED tests/a.py::t1 - x'}
        self._seed_verification('x-env', 'review:1', 'h1',
                                [{'failure_class': 'env', 'exit_code': 1, 'failed_nodes': ['tests/e.py::t']}])
        self._seed_verification('x-old', 'review:2', 'h1', [dict(node)],
                                created_at=time.time() - 8 * 86400)
        self._seed_verification('x-pass', 'review:3', 'h1', [dict(node)], passed=True)
        self.s.sync()
        self.assertNotIn('baseline_failures', self._discover_payload())

    def test_baseline_candidates_rank_by_heads_then_oldest_and_cap(self):
        def check(node):
            return {'failure_class': 'code', 'exit_code': 1,
                    'failed_nodes': [node], 'failed_line': 'FAILED %s - x' % node}
        for i in range(1, 7):  # n1 is the oldest
            self._seed_verification('x%d' % i, 'review:%d' % i, 'h1', [check('tests/n%d.py::t' % i)],
                                    created_at=time.time() - (100 - i) * 60)
        self._seed_verification('x1b', 'review:9', 'h2', [check('tests/n1.py::t')],
                                created_at=time.time() - 30 * 60)
        self.s.sync()
        nodes = [c['node'] for c in self._discover_payload()['baseline_failures']]
        self.assertEqual(nodes, ['tests/n1.py::t', 'tests/n2.py::t', 'tests/n3.py::t',
                                 'tests/n4.py::t', 'tests/n5.py::t'])

    def test_verify_counts_env_failures_and_status_exposes_them(self):
        repo = Path(self.tmp.name) / 'repo'
        repo.mkdir()
        git(repo, 'init', '-b', 'main')
        git(repo, 'config', 'user.email', 'test@example.invalid')
        git(repo, 'config', 'user.name', 'Test')
        (repo / 'README.md').write_text('x\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'initial')
        trees = Path(self.tmp.name) / 'trees'
        wt = trees / 'issue-1'
        git(repo, 'worktree', 'add', '-b', 'fix/mocx/bug', str(wt))
        head = git(wt, 'rev-parse', 'HEAD')
        self.cfg.update({'repo_root': str(repo.resolve()), 'worktree_root': str(trees.resolve()),
                         'branch_prefixes': ['feature/mocx/', 'fix/mocx/']})
        self.cfg['verification'] = [[sys.executable, '-c',
                                     "raise ModuleNotFoundError(\"No module named 'radon'\")"]]
        pr = self.pr()
        pr['head'] = {'sha': head, 'ref': 'fix/mocx/bug'}
        self.platform.data['prs'] = [pr]
        self.s.sync()
        execution = self.s.claim('reviewer', 'review-worker')
        self.s.bind(execution['id'], execution['token'], str(wt), 'fix/mocx/bug')
        result = self.s.verify(execution['id'], execution['token'])
        self.assertFalse(result['passed'])
        self.assertEqual(result['env_signature'], 'no-module:radon')
        self.assertEqual(result['prior_env_failures'], 0)
        result = self.s.verify(execution['id'], execution['token'])
        self.assertEqual(result['prior_env_failures'], 1)
        status = self.s.status()
        self.assertEqual(status['env_failures']['no-module:radon']['count'], 2)
        # A code-class failure is not an environment failure: nothing is counted.
        self.cfg['verification'] = [[sys.executable, '-c', 'raise SystemExit(1)']]
        result = self.s.verify(execution['id'], execution['token'])
        self.assertIsNone(result['env_signature'])
        self.assertEqual(result['prior_env_failures'], 0)
        self.assertEqual(self.s.status()['env_failures']['no-module:radon']['count'], 2)

    def test_remote_authorization_reports_enabled_without_marker(self):
        """A registry-authorized (remote) service reports enabled=true even
        with no local marker; the marker is the legacy single-machine path
        (issue #136)."""
        remote_dir = Path(self.tmp.name) / 'remote-state'
        remote_dir.mkdir()
        cfg = dict(self.cfg, state_dir=str(remote_dir))
        remote = Service(cfg, self.platform,
                         authorization={'host': 'cp-server', 'managed_by': 'registry'})
        self._fresh_services.append(remote)
        self.assertFalse((remote_dir / 'machine-enabled.json').is_file())
        self.assertTrue(remote.status()['enabled'])
        # The local service (marker-based) still reports enabled after enable().
        self.assertTrue(self.s.status()['enabled'])

    def test_post_merge_close_requires_worktree_verification(self):
        self.platform.data['issues'] = [self.issue()]
        merged = self.pr()
        merged['merged'] = True
        merged['state'] = 'closed'
        self.platform.data['prs'] = [merged]
        self.s.sync()
        item = self.s.store.items('handler')[0]
        execution = self.s.claim('handler', 'handler-worker', item_id=item['id'])
        self.s.report(execution['id'], execution['token'], {'decision': 'RESOLVED',
                      'reviewer': 'independent', 'summary': 'merged fix', 'action': 'close_issue',
                      'payload': {'number': 1}})
        with self.assertRaisesRegex(ValueError, 'POST_MERGE_WORKTREE_REQUIRED'):
            self.s.publish(execution['id'], execution['token'])

    def test_discover_close_issue_round_trips(self):
        s, p = self._fresh({})
        e = self._discover_close(s, p)
        self._discover_close_report(s, e)
        s.publish(e['id'], e['token'])
        action, payload, _ = p.writes[-1]
        self.assertEqual('close_issue', action)
        self.assertEqual(1, payload['number'])
        self.assertIn('0 hits', payload['body'])
        self.assertIn('> 🤖 本条由 code-loop **discover** 循环自动生成', payload['body'])
        self.assertIn('<!-- code-loop-agent: discover -->', payload['body'])
        self.assertIn('<!-- code-loop:', payload['body'], 'opcode marker must remain')

    def test_discover_close_refuses_decision_class_targets(self):
        variants = [dict(self.issue(), labels=['needs-human']),
                    dict(self.issue(), labels=['[❓needs-human]']),
                    dict(self.issue(), title='[Gap] product'),
                    dict(self.issue(), title='[❓needs-human] 选型')]
        for target in variants:
            s, p = self._fresh({})
            e = self._discover_close(s, p, issue=target)
            with self.assertRaisesRegex(ValueError, 'CLOSE_TARGET_FORBIDDEN'):
                self._discover_close_report(s, e)

    def test_discover_close_refuses_in_flight_and_post_merge_targets(self):
        s, p = self._fresh({})
        e = self._discover_close(s, p, prs=[self.pr()])
        with self.assertRaisesRegex(ValueError, 'CLOSE_TARGET_IN_FLIGHT'):
            self._discover_close_report(s, e)
        merged_linked = self.pr()
        merged_linked.update(state='closed', merged=True)
        s, p = self._fresh({})
        e = self._discover_close(s, p, prs=[merged_linked])
        with self.assertRaisesRegex(ValueError, 'CLOSE_TARGET_POST_MERGE'):
            self._discover_close_report(s, e)
        # Broken linkage (merged PR whose body never referenced the issue):
        # exactly the residue this fallback exists for — closing is allowed.
        broken = self.pr()
        broken.update(state='closed', merged=True, body='plain body')
        s, p = self._fresh({})
        e = self._discover_close(s, p, prs=[broken])
        self._discover_close_report(s, e)
        s.publish(e['id'], e['token'])
        self.assertEqual('close_issue', p.writes[-1][0])

    def test_discover_close_refuses_closed_target(self):
        s, p = self._fresh({})
        e = self._discover_close(s, p, issue=dict(self.issue(), state='closed'))
        with self.assertRaisesRegex(ValueError, 'ISSUE_NOT_OPEN'):
            self._discover_close_report(s, e)
        # Target state outranks evidence emptiness: a closed issue is
        # categorically not closable, whatever the payload carries.
        with self.assertRaisesRegex(ValueError, 'ISSUE_NOT_OPEN'):
            s.report(e['id'], e['token'], {'decision': 'CONFIRMED', 'reviewer': 'verifier',
                          'summary': 'x', 'action': 'close_issue',
                          'payload': {'number': 1, 'body': '   '}})

    def test_discover_close_requires_evidence_body(self):
        s, p = self._fresh({})
        e = self._discover_close(s, p)
        with self.assertRaisesRegex(ValueError, 'INVALID_PUBLICATION_PAYLOAD'):
            s.report(e['id'], e['token'], {'decision': 'CONFIRMED', 'reviewer': 'verifier',
                          'summary': 'x', 'action': 'close_issue', 'payload': {'number': 1}})
        with self.assertRaisesRegex(ValueError, 'CLOSE_EVIDENCE_REQUIRED'):
            s.report(e['id'], e['token'], {'decision': 'CONFIRMED', 'reviewer': 'verifier',
                          'summary': 'x', 'action': 'close_issue',
                          'payload': {'number': 1, 'body': '   '}})
        with self.assertRaisesRegex(ValueError, 'INVALID_DECISION'):
            s.report(e['id'], e['token'], {'decision': 'RESOLVED', 'reviewer': 'verifier',
                          'summary': 'x', 'action': 'close_issue',
                          'payload': {'number': 1, 'body': 'evidence'}})

    def test_close_issue_payload_cannot_carry_body(self):
        """handler close stays body-less: its evidence lives in the independent
        POST_MERGE worktree/verification chain, not in the close payload
        (discover's evidence-carrying close is authorized separately)."""
        merged = self.pr()
        merged['merged'] = True
        merged['state'] = 'closed'
        self.platform.data['issues'] = [self.issue()]
        self.platform.data['prs'] = [merged]
        self.s.sync()
        item = self.s.store.items('handler')[0]
        execution = self.s.claim('handler', 'handler-worker', item_id=item['id'])
        with self.assertRaisesRegex(ValueError, 'INVALID_PUBLICATION_PAYLOAD'):
            self.s.report(execution['id'], execution['token'], {
                'decision': 'RESOLVED', 'reviewer': 'independent', 'summary': 'merged',
                'action': 'close_issue', 'payload': {'number': 1, 'body': 'sneaky'}})

    def test_own_marker_in_issue_or_pr_does_not_delete_entity(self):
        marker = '<!-- code-loop:known -->'
        item = self.s.store.put_item('marker-test', 'discover', 'v1', {'task': 'DISCOVERY'}, ['issue-discover'])
        execution = self.s.store.claim('discover', 'worker')
        self.s.store.db.execute("INSERT INTO outbox(id,execution_id,action,payload,state,receipt) VALUES (?,?,?,?,?,?)",
                                ('known', execution['id'], 'create_issue', '{}', 'SENT', '{}'))
        issue = {'number': 9, 'title': 'x', 'body': 'created ' + marker, 'comments': []}
        pr = {'number': 10, 'title': 'p', 'body': 'created ' + marker, 'head': {}, 'comments': []}
        self.assertEqual(self.s.source([issue, pr]), [issue, pr])

    def test_discover_issue_receipt_comment_remains_input_but_review_receipt_does_not(self):
        issue_item = self.s.store.put_item('marker-issue', 'discover', 'v1', {'task': 'DISCOVERY'}, ['issue-discover'])
        issue_execution = self.s.store.claim('discover', 'discoverer')
        self.s.store.db.execute("INSERT INTO outbox(id,execution_id,action,payload,state,receipt) VALUES (?,?,?,?,?,?)",
                                ('issue-op', issue_execution['id'], 'comment_issue', '{}', 'SENT', '{}'))
        review_item = self.s.store.put_item('marker-review', 'reviewer', 'v1', {'task': 'PR_REVIEW'}, ['pr-1'])
        review_execution = self.s.store.claim('reviewer', 'reviewer')
        self.s.store.db.execute("INSERT INTO outbox(id,execution_id,action,payload,state,receipt) VALUES (?,?,?,?,?,?)",
                                ('review-op', review_execution['id'], 'review_comment', '{}', 'SENT', '{}'))
        issue_comment = {'id': 1, 'body': '<!-- code-loop:issue-op -->', 'user': {}}
        review_comment = {'id': 2, 'body': '<!-- code-loop:review-op -->', 'user': {}}
        self.assertEqual(self.s.source([issue_comment, review_comment]), [issue_comment])


if __name__ == '__main__':
    unittest.main()
