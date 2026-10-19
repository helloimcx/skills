#!/usr/bin/env python3
"""Reviewer approval gate: an Approved verdict must carry a coverage report of
the reviewed diff (report_coverage.py), and the exam-side plumbing stays out of
this file — the reviewer gate is where coverage becomes a merge precondition."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.cp.service import Service


class Platform:
    def __init__(self):
        self.data = {'base_sha': 'base1', 'issues': [], 'prs': []}
        self.writes = []
        self.known_received = []
        self.fail = False
        self.uncertain = False
    def snapshot(self, known=None):
        self.known_received.append(known)
        if self.fail:
            raise ValueError('network failed')
        return copy.deepcopy(self.data)

    def raw_file(self, path, ref):
        # No repository exam in this fixture: the machine config stands.
        return None
    def pr_files(self, number):
        return []
    def find(self, marker, action, payload):
        return next((r for a, p, r in self.writes if marker in p.get('body', '')), None)
    def write(self, action, payload):
        receipt = {'number': 100 + len(self.writes)}
        self.writes.append((action, payload, receipt))
        if self.uncertain:
            raise ValueError('response lost')
        return receipt


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], stderr=subprocess.DEVNULL, text=True).strip()


class CoverageGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._tmps = []
        self._fresh_services = []
        # The observer store is machine-global (~/.code-loop); redirect the
        # data home so each case gets its own empty store and no case can
        # read facts another case crawled.
        self._home = os.environ.get('CODE_LOOP_HOME')
        os.environ['CODE_LOOP_HOME'] = self.tmp.name
        self.cfg = {'state_dir': self.tmp.name, 'base': 'main',
                    'platform': {'repo': 'a/b'}, 'verification': [], 'required_checks': [],
                    'max_parallel': 3, 'lease_seconds': 1800, 'project_key': 'a__b',
                    'snapshot_ttl': 300, 'discovery_window_seconds': 10800}
        self.platform = Platform()
        self.s = Service(self.cfg, self.platform)
        self.s.enable()

    def tearDown(self):
        self.s.store.db.close()
        for service in self._fresh_services:
            service.store.db.close()
        for tmp in self._tmps:
            tmp.cleanup()
        self.tmp.cleanup()
        if self._home is None:
            os.environ.pop('CODE_LOOP_HOME', None)
        else:
            os.environ['CODE_LOOP_HOME'] = self._home

    def _fresh(self, cfg=None):
        """Isolated Service+Platform per case: syncs cancel stale items and
        leases persist across cases, so sharing one Service couples them."""
        tmp = tempfile.TemporaryDirectory()
        self._tmps.append(tmp)
        c = dict(self.cfg, state_dir=tmp.name,
                 project_key='test__' + Path(tmp.name).name)
        c.update(cfg or {})
        platform = Platform()
        service = Service(c, platform)
        service.enable()
        self._fresh_services.append(service)
        return service, platform

    def _worktree(self, origin=False):
        tmp = tempfile.TemporaryDirectory()
        self._tmps.append(tmp)
        repo = Path(tmp.name) / 'repo'
        repo.mkdir()
        git(repo, 'init', '-b', 'main')
        git(repo, 'config', 'user.email', 'test@example.invalid')
        git(repo, 'config', 'user.name', 'Test')
        (repo / 'README.md').write_text('x\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'initial')
        if origin:
            bare = Path(tmp.name) / 'origin.git'
            git(repo, 'init', '--bare', str(bare))
            git(repo, 'remote', 'add', 'origin', str(bare))
            git(repo, 'push', '-u', 'origin', 'main')
        trees = Path(tmp.name) / 'trees'
        wt = trees / 'issue-1'
        git(repo, 'worktree', 'add', '-b', 'fix/mocx/bug', str(wt))
        return str(wt), {'repo_root': str(repo.resolve()), 'worktree_root': str(trees.resolve()),
                         'branch_prefixes': ['feature/mocx/', 'fix/mocx/'],
                         'verification': [[sys.executable, '-c', 'pass']]}

    def issue(self):
        return {'number': 1, 'state': 'open', 'title': '[Content] bug', 'body': 'evidence',
                'labels': [], 'comments': []}

    def pr(self):
        return {'number': 2, 'state': 'open', 'title': 'fix', 'body': 'Closes #1',
                'head': {'sha': 'head1', 'ref': 'fix/mocx/bug'},
                'base': {'sha': 'base1', 'ref': 'main'}, 'merged': False,
                'mergeable': True, 'draft': False, 'comments': [], 'reviews': [], 'checks': []}

    GOOD_COVERAGE = {
        'total': 4, 'ok': True, 'rate': 0.5,
        'reviewed': ['app.py', 'pkg/util.go'],
        'skipped': [{'path': 'package-lock.json', 'reason': 'lockfile'},
                    {'path': 'dist/bundle.js', 'reason': 'generated bundle'}],
    }

    def test_reviewer_approved_requires_coverage(self):
        # helper 内部首次 report 即因缺 coverage 被拒（COVERAGE_REQUIRED）
        with self.assertRaisesRegex(ValueError, 'COVERAGE_REQUIRED'):
            self._approved_reviewer(coverage=None)

    def test_reviewer_first_verdict_without_action_rejected(self):
        """首次评审的判词必须带 action=review_comment（issue #223）：无 action
        的 report 会直接 SUCCEEDED（D1），PR 平台侧永远收不到评审评论。三种
        判词（Approved/Changes requested/Validation failed）全部锁定——此前
        只测了 Approved，另两种漏失。"""
        for decision in ('Approved for manual merge', 'Changes requested', 'Validation failed'):
            with self.subTest(decision=decision):
                wt, cfg = self._worktree()
                s, p = self._fresh(cfg)
                pr = self.pr()
                pr['body'] = 'fix'
                pr['head'] = {'sha': git(wt, 'rev-parse', 'HEAD'), 'ref': 'fix/mocx/bug'}
                p.data['issues'] = []
                p.data['prs'] = [pr]
                s.sync()
                item = [x for x in s.store.items('reviewer') if x['state'] == 'READY'][0]
                e = s.claim('reviewer', 'worker', item_id=item['id'])
                s.bind(e['id'], e['token'], wt, 'fix/mocx/bug')
                s.verify(e['id'], e['token'])
                result = {'decision': decision, 'reviewer': 'verifier',
                          'summary': 'all good', 'coverage': self.GOOD_COVERAGE}
                with self.assertRaisesRegex(ValueError, 'REVIEW_COMMENT_REQUIRED'):
                    s.report(e['id'], e['token'], result)

    def test_reviewer_rederived_verdict_after_head_change_without_action_rejected(self):
        """head 前进后的重复派生项不允许无 action report——SENT 评论 body 是旧 head
        的 'Head: <old_sha>'，与新 head 不匹配，放行即把新判词静默丢弃（D1）且
        terminal_verdicts 按 head 匹配不到，PR 卡死评审态（#223 回归，曾固化
        错误行为）。"""
        wt, cfg = self._worktree()
        s, p = self._fresh(cfg)
        pr = self.pr()
        pr['body'] = 'fix'
        pr['head'] = {'sha': git(wt, 'rev-parse', 'HEAD'), 'ref': 'fix/mocx/bug'}
        p.data['issues'] = []
        p.data['prs'] = [pr]
        s.sync()
        item = [x for x in s.store.items('reviewer') if x['state'] == 'READY'][0]
        e = s.claim('reviewer', 'worker', item_id=item['id'])
        s.bind(e['id'], e['token'], wt, 'fix/mocx/bug')
        s.verify(e['id'], e['token'])
        first = {'decision': 'Approved for manual merge', 'reviewer': 'verifier',
                 'summary': 'all good', 'action': 'review_comment',
                 'payload': {'number': 2, 'body': 'review notes'},
                 'coverage': self.GOOD_COVERAGE}
        s.report(e['id'], e['token'], first)
        s.publish(e['id'], e['token'])
        # PR head 前进 → CP 重新派生 review 项；新 execution 无 action 应被拒
        (Path(wt) / 'README.md').write_text('x\nchanged\n')
        git(wt, 'add', '.')
        git(wt, 'commit', '-m', 'change')
        pr['head'] = {'sha': git(wt, 'rev-parse', 'HEAD'), 'ref': 'fix/mocx/bug'}
        s.cfg['snapshot_ttl'] = 0  # 绕过 observer 快照缓存，强制重爬
        s.sync()
        s.cfg['snapshot_ttl'] = 300  # 恢复，避免 claim 的 fresh() 判 stale
        item2 = [x for x in s.store.items('reviewer') if x['state'] == 'READY'][0]
        e2 = s.claim('reviewer', 'worker', item_id=item2['id'])
        s.bind(e2['id'], e2['token'], wt, 'fix/mocx/bug')
        s.verify(e2['id'], e2['token'])
        confirm = {'decision': 'Approved for manual merge', 'reviewer': 'verifier2',
                   'summary': 'confirm', 'coverage': self.GOOD_COVERAGE}
        with self.assertRaisesRegex(ValueError, 'REVIEW_COMMENT_REQUIRED'):
            s.report(e2['id'], e2['token'], confirm)

    def test_reviewer_rederived_verdict_head_unchanged_passes(self):
        """重复派生项（PR head 未变 + 该 head 已有 SENT review_comment）允许无
        action report 确认既有结论——SENT 评论 body 由 prepare_publish 确定性
        构造、必含匹配的 'Head: <sha>' 行（issue #223 的合法场景，此前未被
        测试直接锁定）。"""
        from codeloop.cp import gate as gate_mod
        head = 'a' * 40
        sent = [{'number': 2, 'body': 'Decision: Approved for manual merge\nHead: ' + head
                                        + '\nBase: base1\nCoverage: 1/1\nnotes'}]
        service = self._gate_service(sent)
        item = {'kind': 'reviewer'}
        payload = {'pr': {'number': 2, 'head': {'sha': head}}}
        result = {'decision': 'Approved for manual merge', 'reviewer': 'verifier',
                  'summary': 'confirm', 'coverage': None}
        gate_mod._validate_review_comment_required(service, item, payload, result)

    def test_reviewer_rederived_verdict_old_head_does_not_exempt(self):
        """既有 SENT 评论匹配的 head 与当前 head 不同 → 该评论不能豁免当前 head，
        无 action 必须被拒（#223 防错合并的核心：旧 head 的 Approved 不得盖过
        新 head 的判词）。"""
        from codeloop.cp import gate as gate_mod
        old_head, new_head = 'a' * 40, 'b' * 40
        body = 'Decision: Approved for manual merge\nHead: ' + old_head + '\nBase: base1\n'
        service = self._gate_service([{'number': 2, 'body': body}])
        item = {'kind': 'reviewer'}
        payload = {'pr': {'number': 2, 'head': {'sha': new_head}}}
        result = {'decision': 'Changes requested', 'reviewer': 'verifier', 'summary': 'x'}
        with self.assertRaisesRegex(ValueError, 'REVIEW_COMMENT_REQUIRED'):
            gate_mod._validate_review_comment_required(service, item, payload, result)

    def test_reviewer_illegal_action_deferred_to_authorize(self):
        """reviewer 带越权 action（如 create_pr）时校验层不得以误导的
        REVIEW_COMMENT_REQUIRED 抢先拒绝——应留给 authorize 报
        ROLE_ACTION_FORBIDDEN（#223 次要项）。"""
        from codeloop.cp import gate as gate_mod
        service = self._gate_service([])  # 无任何 SENT 评论，仍须放行到 authorize
        item = {'kind': 'reviewer'}
        payload = {'pr': {'number': 2, 'head': {'sha': 'c' * 40}}}
        result = {'decision': 'Approved for manual merge', 'reviewer': 'verifier',
                  'summary': 'x', 'action': 'create_pr',
                  'payload': {'title': 't', 'body': 'b', 'head': 'x', 'base': 'main'}}
        gate_mod._validate_review_comment_required(service, item, payload, result)  # no raise
        with self.assertRaisesRegex(ValueError, 'ROLE_ACTION_FORBIDDEN'):
            self._report_full_authorization(action='create_pr')

    def _gate_service(self, sent_rows):
        """最小假 service：gate 函数唯一用到的 store 面就是
        ``db.execute(...).fetchall()`` 取 SENT outbox 行。"""
        class _Rows:
            def __init__(self, rows):
                self._rows = rows
            def fetchall(self):
                return [(json.dumps(row),) for row in self._rows]
        class _Db:
            def __init__(self, rows):
                self._rows = rows
            def execute(self, sql):
                return _Rows(self._rows)
        class _Store:
            def __init__(self, rows):
                self.db = _Db(rows)
        class _Service:
            def __init__(self, rows):
                self.store = _Store(rows)
        return _Service(sent_rows)

    def _report_full_authorization(self, action):
        """完整流程断言：reviewer 带越权 action 时，authorize 层拒绝。"""
        wt, cfg = self._worktree()
        s, p = self._fresh(cfg)
        pr = self.pr()
        pr['body'] = 'fix'
        pr['head'] = {'sha': git(wt, 'rev-parse', 'HEAD'), 'ref': 'fix/mocx/bug'}
        p.data['issues'] = []
        p.data['prs'] = [pr]
        s.sync()
        item = [x for x in s.store.items('reviewer') if x['state'] == 'READY'][0]
        e = s.claim('reviewer', 'worker', item_id=item['id'])
        s.bind(e['id'], e['token'], wt, 'fix/mocx/bug')
        s.verify(e['id'], e['token'])
        result = {'decision': 'Approved for manual merge', 'reviewer': 'verifier',
                  'summary': 'x', 'action': action,
                  'payload': {'title': 't', 'body': 'b', 'head': 'fix/mocx/bug', 'base': 'main'},
                  'coverage': self.GOOD_COVERAGE}
        return s.report(e['id'], e['token'], result)

    def test_reviewer_approved_coverage_empty_diff_passes(self):
        """空 diff：total=0 → rate=1.0，覆盖度核算合法通过。"""
        s, p, e = self._approved_reviewer(
            coverage={'total': 0, 'reviewed': [], 'skipped': [],
                      'ok': True, 'rate': 1.0})
        s.publish(e['id'], e['token'])
        self.assertIn('Coverage: 0/0 reviewed (100%), 0 skipped',
                      p.writes[-1][1]['body'])

    def test_reviewer_approved_coverage_ok_passes(self):
        s, p, e = self._approved_reviewer(coverage=self.GOOD_COVERAGE)
        # report 已通过；再 publish 校验覆盖度行确定性渲染
        s.publish(e['id'], e['token'])
        body = p.writes[-1][1]['body']
        self.assertIn('Coverage: 2/4 reviewed (50%), 2 skipped', body)

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

    def test_reviewer_approved_coverage_incomplete_rejected(self):
        bad = dict(self.GOOD_COVERAGE, ok=False)
        with self.assertRaisesRegex(ValueError, 'COVERAGE_INCOMPLETE'):
            self._approved_reviewer(coverage=bad)

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

    def _approved_reviewer(self, coverage=None):
        """完整 Approved reviewer 流：claim→bind→verify→report(带 coverage)。

        返回 (service, platform, execution)；report 结果可再覆盖（换 coverage
        再 report 一次做断言，终态由 outbox 推导不影响）。
        """
        wt, cfg = self._worktree()
        s, p = self._fresh(cfg)
        pr = self.pr()
        pr['body'] = 'fix'  # 无 Closes 链接，不占 Issue 锁
        pr['head'] = {'sha': git(wt, 'rev-parse', 'HEAD'), 'ref': 'fix/mocx/bug'}
        p.data['issues'] = []
        p.data['prs'] = [pr]
        s.sync()
        item = [x for x in s.store.items('reviewer') if x['state'] == 'READY'][0]
        e = s.claim('reviewer', 'worker', item_id=item['id'])
        s.bind(e['id'], e['token'], wt, 'fix/mocx/bug')
        s.verify(e['id'], e['token'])
        result = {'decision': 'Approved for manual merge', 'reviewer': 'verifier',
                  'summary': 'all good', 'action': 'review_comment',
                  'payload': {'number': 2, 'body': 'review notes'}}
        if coverage is not None:
            result['coverage'] = coverage
        s.report(e['id'], e['token'], result)
        return s, p, e

    GOOD_COVERAGE = {
        'total': 4, 'ok': True, 'rate': 0.5,
        'reviewed': ['app.py', 'pkg/util.go'],
        'skipped': [{'path': 'package-lock.json', 'reason': 'lockfile'},
                    {'path': 'dist/bundle.js', 'reason': 'generated bundle'}],
    }


if __name__ == '__main__':
    unittest.main()
