#!/usr/bin/env python3
"""Repository exam: the verification commands and required checks that decide
whether a change passes are read from the project repository
(``.code-loop/verify.json``) at the observed base commit, with the machine
config as the migration fallback track. The runner-side evidence they produce
(environment-failure classification) lives here too, next to the exam."""
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

from codeloop.cp import runtime
from codeloop.cp.gitea import GiteaError
from codeloop.cp.exam import GUARD_NOTE as EXAM_GUARD_NOTE
from codeloop.cp.service import Service


class Platform:
    def __init__(self):
        self.data = {'base_sha': 'base1', 'issues': [], 'prs': []}
        self.writes = []
        self.fail = False
        self.files = {}          # repository paths -> raw content (absent = 404)
        self.raw_calls = []      # (path, ref) of every exam read
        self.changed_files = []  # PR diff paths (exam guard)

    def snapshot(self, known=None):
        if self.fail:
            raise ValueError('network failed')
        return copy.deepcopy(self.data)

    def raw_file(self, path, ref):
        self.raw_calls.append((path, ref))
        return self.files.get(path)

    def pr_files(self, number):
        return list(self.changed_files)

    def find(self, marker, action, payload):
        return next((r for a, p, r in self.writes if marker in p.get('body', '')), None)

    def write(self, action, payload):
        receipt = {'number': 100 + len(self.writes)}
        self.writes.append((action, payload, receipt))
        return receipt


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], stderr=subprocess.DEVNULL,
                                   text=True).strip()


class ExamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._tmps = []
        self._services = []
        # The observer store is machine-global (~/.code-loop); redirect the
        # data home so each case gets its own empty store.
        self._home = os.environ.get('CODE_LOOP_HOME')
        os.environ['CODE_LOOP_HOME'] = self.tmp.name
        # A real repository with one worktree: the exam resolution tests only
        # need the config, the evidence test runs a real verification.
        self.repo = Path(self.tmp.name) / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-b', 'main')
        git(self.repo, 'config', 'user.email', 't@i')
        git(self.repo, 'config', 'user.name', 'T')
        (self.repo / 'README.md').write_text('x\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'initial')
        self.trees = Path(self.tmp.name) / 'trees'
        self.wt = self.trees / 'issue-1'
        git(self.repo, 'worktree', 'add', '-b', 'fix/mocx/bug', str(self.wt))
        self.cfg = {'state_dir': self.tmp.name, 'base': 'main', 'project_key': 'a__b',
                    'platform': {'repo': 'a/b'}, 'verification': [], 'required_checks': [],
                    'max_parallel': 3, 'lease_seconds': 1800, 'snapshot_ttl': 300,
                    'discovery_window_seconds': 10800, 'repo_root': str(self.repo.resolve()),
                    'worktree_root': str(self.trees.resolve()),
                    'branch_prefixes': ['feature/mocx/', 'fix/mocx/']}
        self.platform = Platform()
        self.s = Service(self.cfg, self.platform)
        self.s.enable()

    def tearDown(self):
        self.s.store.db.close()
        for service in self._services:
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
        c = dict(self.cfg, state_dir=tmp.name, project_key='test__' + Path(tmp.name).name)
        c.update(cfg or {})
        platform = Platform()
        service = Service(c, platform)
        service.enable()
        self._services.append(service)
        return service, platform

    def expire_observer(self):
        """Age the shared store so the next sync re-crawls — the test
        equivalent of the observation TTL elapsing."""
        db = sqlite3.connect(str(Path(os.environ['CODE_LOOP_HOME']) / 'observer.sqlite'))
        db.execute("UPDATE meta SET value=? WHERE key='synced_at'", (json.dumps(0),))
        db.commit()
        db.close()

    def issue(self):
        return {'number': 1, 'state': 'open', 'title': '[Content] bug', 'body': 'evidence',
                'labels': [], 'comments': []}

    def pr(self):
        return {'number': 2, 'state': 'open', 'title': 'fix', 'body': 'Closes #1',
                'head': {'sha': 'head1', 'ref': 'fix/mocx/bug'},
                'base': {'sha': 'base1', 'ref': 'main'}, 'merged': False,
                'mergeable': True, 'draft': False, 'comments': [], 'reviews': [], 'checks': []}

    def test_repo_exam_overrides_machine_config(self):
        """仓库考题生效时机器两键整体让位：policy 携带仓库值，source 反映来源。"""
        s, p = self._fresh({'verification': [['python', '-m', 'pytest']],
                            'required_checks': ['legacy']})
        p.files[runtime.EXAM_PATH] = json.dumps(
            {'verification': [['python', '-m', 'pytest', 'tests/unit', '-q']],
             'required_checks': ['ci/build']})
        s.sync()
        self.assertEqual([['python', '-m', 'pytest', 'tests/unit', '-q']],
                         s.policy()['verification'])
        self.assertEqual(['ci/build'], s.policy()['required_checks'])
        self.assertEqual('repo', s.status()['verification_source'])
        self.assertEqual([(runtime.EXAM_PATH, 'base1')], p.raw_calls)

    def test_repo_exam_is_read_only_at_the_observed_base(self):
        """防"PR 改自己考题"：快照里有 open PR 时读取 ref 恒为 base_sha，
        PR head 前进（甚至带篡改的同名文件）都不触发重读。"""
        s, p = self._fresh({})
        p.data['issues'] = [self.issue()]
        p.data['prs'] = [self.pr()]
        p.files[runtime.EXAM_PATH] = json.dumps({'verification': [['python', '-c', 'pass']]})
        s.sync()
        self.assertEqual([(runtime.EXAM_PATH, 'base1')], p.raw_calls)
        self.assertEqual('head1', p.data['prs'][0]['head']['sha'])
        self.assertEqual([['python', '-c', 'pass']], s.policy()['verification'])
        p.data['prs'][0]['head']['sha'] = 'head2'
        self.expire_observer()
        s.sync()
        self.assertEqual([(runtime.EXAM_PATH, 'base1')], p.raw_calls)

    def test_missing_repo_exam_falls_back_to_machine_track(self):
        """404 双轨回退：策略与部署前逐字节一致（零扰动）——键集合不变、
        取值仍来自机器配置，source=machine。"""
        s, p = self._fresh({'verification': [['python', '-m', 'pytest']],
                            'required_checks': ['legacy']})
        s.sync()
        self.assertEqual('machine', s.status()['verification_source'])
        policy = s.policy()
        self.assertEqual({'base', 'verification', 'required_checks', 'branch_prefixes',
                          'issue_scopes', 'policy_version'}, set(policy))
        self.assertEqual([['python', '-m', 'pytest']], policy['verification'])
        self.assertEqual(['legacy'], policy['required_checks'])

    def test_invalid_repo_exam_fails_sync_and_blocks_claims(self):
        """坏考题 fail closed：sync 失败、sync_error 置位、claim 拒绝，
        绝不静默回退机器值。"""
        s, p = self._fresh({'verification': [['python', '-m', 'pytest']]})
        p.files[runtime.EXAM_PATH] = json.dumps({'verification': 'pytest -q'})
        with self.assertRaisesRegex(ValueError, 'VERIFICATION_MUST_BE_ARGV'):
            s.sync()
        self.assertEqual('ValueError', s.meta('sync_error'))
        # claim 内建 re-sync，坏考题在领取处再次响亮失败——不会发出任何工作项。
        with self.assertRaisesRegex(ValueError, 'VERIFICATION_MUST_BE_ARGV'):
            s.claim('discover', 'worker')

    def test_unreachable_exam_fails_sync_instead_of_falling_back(self):
        """服务中断 ≠ 文件不存在：raw 读取错误必须 fail closed。"""
        s, p = self._fresh({})

        def boom(path, ref):
            raise GiteaError('GET raw file failed (URLError)')

        p.raw_file = boom
        with self.assertRaises(GiteaError):
            s.sync()
        self.assertIn(s.meta('sync_error'), ('GitHubError', 'GiteaError'))

    def test_exam_change_with_base_advance_cancels_inflight_execution(self):
        """main 前进（含考题合并）→ 全量重排：在途执行 STALE_INPUT，
        下一次 claim 拿到新考题；raw 按新 base 重读（缓存按 sha）。"""
        s, p = self._fresh({})
        p.data['issues'] = [self.issue()]
        p.files[runtime.EXAM_PATH] = json.dumps({'verification': [['python', '-c', 'pass']]})
        s.sync()
        e = s.claim('discover', 'worker')
        p.data['base_sha'] = 'base2'
        p.files[runtime.EXAM_PATH] = json.dumps({'verification': [['python', '-c', 'pass', '--new']]})
        self.expire_observer()
        s.sync()
        with self.assertRaisesRegex(ValueError, 'STALE_INPUT'):
            s.report(e['id'], e['token'],
                     {'decision': 'CONFIRMED', 'reviewer': 'verifier', 'summary': 'stale round'})
        self.assertEqual([['python', '-c', 'pass', '--new']], s.policy()['verification'])
        self.assertEqual([(runtime.EXAM_PATH, 'base1'), (runtime.EXAM_PATH, 'base2')], p.raw_calls)

    def test_source_transition_is_recorded_as_an_event(self):
        s, p = self._fresh({})
        s.sync()
        p.files[runtime.EXAM_PATH] = json.dumps({'verification': []})
        p.data['base_sha'] = 'base2'
        self.expire_observer()
        s.sync()
        rows = s.store.db.execute(
            "SELECT payload FROM events WHERE kind='exam.source'").fetchall()
        self.assertEqual(1, len(rows))
        self.assertEqual('repo', json.loads(rows[0]['payload'])['source'])

    def _approved_reviewer(self, service, platform, checks):
        """完整 Approved reviewer 流（claim→bind→verify→report 带覆盖度），
        返回 execution；发布由调用方触发以便断言 gate 结论。"""
        head = git(self.wt, 'rev-parse', 'HEAD')
        pr = self.pr()
        pr['body'] = 'fix'  # 无 Closes 链接，不占 Issue 锁
        pr['head'] = {'sha': head, 'ref': 'fix/mocx/bug'}
        pr['checks'] = checks
        platform.data['issues'] = []
        platform.data['prs'] = [pr]
        service.sync()
        item = [x for x in service.store.items('reviewer') if x['state'] == 'READY'][0]
        e = service.claim('reviewer', 'worker', item_id=item['id'])
        service.bind(e['id'], e['token'], str(self.wt), 'fix/mocx/bug')
        service.verify(e['id'], e['token'])
        service.report(e['id'], e['token'], {
            'decision': 'Approved for manual merge', 'reviewer': 'verifier', 'summary': 'all good',
            'action': 'review_comment', 'payload': {'number': 2, 'body': 'notes'},
            'coverage': {'total': 0, 'reviewed': [], 'skipped': [], 'ok': True, 'rate': 1.0}})
        return e

    def test_repo_required_checks_gate_the_approval(self):
        """仓库考题的 required_checks 端到端参与 reviewer 放行：statuses 缺失
        或未成功 → REQUIRED_CHECKS_NOT_PASS；观测到成功后放行（AC#7）。"""
        s, p = self._fresh({})
        p.files[runtime.EXAM_PATH] = json.dumps(
            {'verification': [[sys.executable, '-c', 'pass']], 'required_checks': [' ci/build ']})
        e = self._approved_reviewer(s, p, [])
        with self.assertRaisesRegex(ValueError, 'REQUIRED_CHECKS_NOT_PASS'):
            s.publish(e['id'], e['token'])
        # 首字空白被规范化（" ci/build " → ci/build），平台观测到成功后放行。
        s2, p2 = self._fresh({})
        p2.files[runtime.EXAM_PATH] = json.dumps(
            {'verification': [[sys.executable, '-c', 'pass']], 'required_checks': [' ci/build ']})
        e2 = self._approved_reviewer(s2, p2, [{'id': 1, 'context': 'ci/build', 'status': 'success'}])
        s2.publish(e2['id'], e2['token'])
        self.assertEqual(1, len(p2.writes))

    def test_verdict_comment_stamps_guard_when_pr_touches_exam(self):
        """AC#9：PR diff 精确触碰考题路径 → 判词评论由服务端从 API 事实拼装
        固定警示行（位于 opcode marker 之前）+ exam.touched_by_pr 事件。"""
        s, p = self._fresh({'verification': [[sys.executable, '-c', 'pass']]})
        p.changed_files = [runtime.EXAM_PATH, 'src/app.py']
        e = self._approved_reviewer(s, p, [])
        s.publish(e['id'], e['token'])
        body = p.writes[0][1]['body']
        self.assertIn(EXAM_GUARD_NOTE, body)
        self.assertLess(body.index(EXAM_GUARD_NOTE), body.index('<!-- code-loop:'))
        rows = s.store.db.execute(
            "SELECT payload FROM events WHERE kind='exam.touched_by_pr'").fetchall()
        self.assertEqual(1, len(rows))
        self.assertEqual(2, json.loads(rows[0]['payload'])['pr'])

    def test_untouched_or_near_miss_paths_get_no_footnote(self):
        """未触碰 → 无脚注无事件；精确路径判定，前缀/近邻路径不触发。"""
        s, p = self._fresh({'verification': [[sys.executable, '-c', 'pass']]})
        p.changed_files = ['src/app.py', '.code-loop/verify.json.bak',
                           'docs/.code-loop/verify.json']
        e = self._approved_reviewer(s, p, [])
        s.publish(e['id'], e['token'])
        self.assertNotIn('考题护栏', p.writes[0][1]['body'])
        self.assertEqual([], s.store.db.execute(
            "SELECT 1 FROM events WHERE kind='exam.touched_by_pr'").fetchall())

    def test_guard_query_failure_blocks_publish_without_residue(self):
        """files 查询失败 → publish 响亮失败、outbox 零残留（读在 INSERT
        之前）；恢复后同一执行干净重试成功并补上警示行。"""
        s, p = self._fresh({'verification': [[sys.executable, '-c', 'pass']]})
        e = self._approved_reviewer(s, p, [])

        def boom(number):
            raise GiteaError('GET /pulls/2/files failed')

        p.pr_files = boom
        with self.assertRaises(GiteaError):
            s.publish(e['id'], e['token'])
        self.assertEqual([], s.store.db.execute('SELECT 1 FROM outbox').fetchall())
        del p.pr_files  # 服务恢复：撤掉实例级故障桩，类方法重新生效
        p.changed_files = [runtime.EXAM_PATH]
        s.publish(e['id'], e['token'])
        self.assertEqual(1, len(p.writes))
        self.assertIn(EXAM_GUARD_NOTE, p.writes[0][1]['body'])

if __name__ == '__main__':
    unittest.main()
