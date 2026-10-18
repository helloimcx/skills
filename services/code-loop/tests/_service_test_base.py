#!/usr/bin/env python3
"""Shared fixtures for control-plane service tests (issue #192).

The ServiceTests suite outgrew the 800-line single-file red line, so the
Platform fake, the git helper and the ServiceTestsBase harness live here and
both test_control_plane_service.py and
test_control_plane_service_conflicts.py inherit from them. The file name
does not start with test_*, so pytest never collects it as a suite.
"""
import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

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
        self.changed_files = []  # PR diff paths (exam guard)

    def snapshot(self, known=None):
        self.known_received.append(known)
        if self.fail:
            raise ValueError('network failed')
        return copy.deepcopy(self.data)

    def raw_file(self, path, ref):
        # No repository exam in this fixture: the machine config stands.
        return None

    def pr_files(self, number):
        return list(self.changed_files)

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


class ServiceTestsBase(unittest.TestCase):
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

    def _seed_verification(self, exec_id, item_key, head, checks, created_at=None, passed=False):
        db = self.s.store.db
        now = time.time()
        db.execute('INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?,?)',
                   ('i-' + exec_id, item_key, 'reviewer', 'v', '{}', '[]', 'SUCCEEDED', 0, now))
        db.execute('INSERT INTO executions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (exec_id, 'i-' + exec_id, 'w', 't-' + exec_id, now + 99, 'SUCCEEDED',
                    None, None, None, None, 'v', now, None))
        payload = {'passed': passed, 'head': head, 'branch': 'feature/mocx/x', 'checks': checks}
        db.execute('INSERT INTO evidence VALUES (?,?,?,?,?)',
                   ('e-' + exec_id, exec_id, 'verification', json.dumps(payload),
                    now if created_at is None else created_at))

    def _discover_payload(self):
        rows = [i for i in self.s.store.items('discover') if i['state'] == 'READY']
        self.assertEqual(len(rows), 1)
        return json.loads(rows[0]['payload'])

    def _discover_close(self, s, p, issue=None, prs=None):
        p.data['issues'] = [issue or self.issue()]
        p.data['prs'] = prs or []
        s.sync()
        return s.claim('discover', 'inspector')

    def _discover_close_report(self, s, e, number=1, body='resolution evidence: 0 hits'):
        s.report(e['id'], e['token'], {'decision': 'CONFIRMED', 'reviewer': 'verifier',
                  'summary': 'independently re-verified', 'action': 'close_issue',
                  'payload': {'number': number, 'body': body}})

    def _conflict_pr(self):
        """PR #2 mergeable=false → PR_CONFLICT_RESOLUTION repair item."""
        pr = self.pr()
        pr['mergeable'] = False
        return pr