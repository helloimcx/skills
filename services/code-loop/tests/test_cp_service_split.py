#!/usr/bin/env python3
"""Checked variants: the coordinator-side halves of bind/verify/publish must be
drivable with a worker-supplied context and stay behavior-identical to the
local wrappers."""
import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.cp.runtime import run_checks, validate_worktree
from codeloop.cp.service import Service


class Platform:
    def __init__(self):
        self.data = {'base_sha': 'base1', 'issues': [], 'prs': []}
        self.writes = []
        self.uncertain = False

    def snapshot(self, known=None):
        return copy.deepcopy(self.data)

    def raw_file(self, path, ref):
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


class SplitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._tmps = []
        # The observer store is machine-global; keep each case hermetic.
        self._home = os.environ.get('CODE_LOOP_HOME')
        os.environ['CODE_LOOP_HOME'] = self.tmp.name
        repo = Path(self.tmp.name) / 'repo'
        repo.mkdir()
        git(repo, 'init', '-b', 'main')
        git(repo, 'config', 'user.email', 't@i')
        git(repo, 'config', 'user.name', 'T')
        (repo / 'README.md').write_text('x\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'initial')
        bare = Path(self.tmp.name) / 'origin.git'
        git(repo, 'init', '--bare', str(bare))
        git(repo, 'remote', 'add', 'origin', str(bare))
        git(repo, 'push', '-u', 'origin', 'main')
        trees = Path(self.tmp.name) / 'trees'
        self.wt = trees / 'issue-1'
        git(repo, 'worktree', 'add', '-b', 'fix/mocx/bug', str(self.wt))
        self.cfg = {'state_dir': self.tmp.name, 'base': 'main', 'project_key': 'a__b',
                    'platform': {'repo': 'a/b'}, 'verification': [[sys.executable, '-c', 'pass']],
                    'required_checks': [], 'max_parallel': 3,
                    'lease_seconds': 1800, 'snapshot_ttl': 300, 'discovery_window_seconds': 10800,
                    'repo_root': str(repo.resolve()), 'worktree_root': str(trees.resolve()),
                    'branch_prefixes': ['feature/mocx/', 'fix/mocx/']}
        self.platform = Platform()
        self.s = Service(self.cfg, self.platform)
        self.s.enable()

    def tearDown(self):
        self.s.store.db.close()
        for tmp in self._tmps:
            tmp.cleanup()
        self.tmp.cleanup()
        if self._home is None:
            os.environ.pop('CODE_LOOP_HOME', None)
        else:
            os.environ['CODE_LOOP_HOME'] = self._home

    def issue(self):
        return {'number': 1, 'state': 'open', 'title': '[Content] bug', 'body': 'evidence',
                'labels': [], 'comments': []}

    def issue_execution(self):
        self.platform.data['issues'] = [self.issue()]
        self.s.sync()
        item = [x for x in self.s.store.items('discover') if x['state'] == 'READY'][0]
        execution = self.s.claim('discover', 'worker', item_id=item['id'])
        self.s.report(execution['id'], execution['token'],
                      {'decision': 'CONFIRMED', 'reviewer': 'verifier', 'summary': 'proof',
                       'action': 'create_issue',
                       'payload': {'title': '[Content] bug', 'body': 'evidence'}})
        return execution

    def outbox_state(self, op):
        return self.s.store.db.execute('SELECT state FROM outbox WHERE id=?', (op,)).fetchone()['state']

    def test_prepare_then_complete_publishes_once(self):
        execution = self.issue_execution()
        prepared = self.s.prepare_publish(execution['id'], execution['token'])
        self.assertIsNone(prepared['receipt'])
        self.assertFalse(prepared['push_required'])
        self.assertEqual(self.outbox_state(prepared['op']), 'SENDING')
        self.assertEqual(self.platform.writes, [])
        receipt = self.s.complete_publish(execution['id'], execution['token'], prepared['op'])
        self.assertEqual(receipt['number'], 100)
        self.assertEqual(len(self.platform.writes), 1)
        self.assertEqual(self.outbox_state(prepared['op']), 'SENT')
        self.assertEqual(self.s.complete_publish(execution['id'], execution['token'], prepared['op']),
                         receipt)
        self.assertEqual(len(self.platform.writes), 1)

    def test_publish_wrapper_stays_equivalent(self):
        execution = self.issue_execution()
        past = time.time() - 60
        self.s.store.db.execute('UPDATE executions SET expires_at=? WHERE id=?',
                                (past, execution['id']))
        receipt = self.s.publish(execution['id'], execution['token'])
        self.assertEqual(receipt['number'], 100)
        self.assertEqual(len(self.platform.writes), 1)
        # publish is a renewal touchpoint: the lease ends in the future.
        row = self.s.store.db.execute('SELECT expires_at FROM executions WHERE id=?',
                                      (execution['id'],)).fetchone()
        self.assertGreater(row['expires_at'], time.time())
        # D2: the commit derives the terminal state -- publish is the last
        # step of a round, no finish follows.
        state = self.s.store.db.execute('SELECT state FROM executions WHERE id=?',
                                        (execution['id'],)).fetchone()['state']
        self.assertEqual('SUCCEEDED', state)

    def test_prepare_resumes_unknown_via_marker_find(self):
        execution = self.issue_execution()
        self.platform.uncertain = True
        with self.assertRaises(ValueError):
            self.s.publish(execution['id'], execution['token'])
        op = self.s.store.db.execute('SELECT id FROM outbox').fetchone()[0]
        self.assertEqual(self.outbox_state(op), 'UNKNOWN')
        self.platform.uncertain = False
        prepared = self.s.prepare_publish(execution['id'], execution['token'])
        self.assertIsNotNone(prepared['receipt'])
        self.assertEqual(len(self.platform.writes), 1)

    def test_complete_requires_prepared_operation(self):
        execution = self.issue_execution()
        with self.assertRaisesRegex(ValueError, 'PUBLICATION_NOT_PREPARED'):
            self.s.complete_publish(execution['id'], execution['token'], 'no-such-op')

    def test_commit_racing_a_steal_lands_unknown(self):
        execution = self.issue_execution()
        original = self.platform.write
        seen = {}

        def sneaky_write(action, payload):
            receipt = original(action, payload)
            # Simulate a concurrent steal terminalizing this execution in the
            # window between the platform write and the commit transaction.
            seen['op'] = self.s.store.db.execute(
                "SELECT id FROM outbox WHERE state='SENDING'").fetchone()[0]
            self.s.store.db.execute("UPDATE executions SET state='CANCELLED' WHERE id=?",
                                    (execution['id'],))
            return receipt

        self.platform.write = sneaky_write
        with self.assertRaisesRegex(ValueError, 'PUBLICATION_UNKNOWN'):
            self.s.publish(execution['id'], execution['token'])
        self.assertEqual('UNKNOWN', self.outbox_state(seen['op']))

    def test_push_failure_lands_unknown_and_never_rewrites(self):
        self.platform.data['issues'] = [self.issue()]
        self.s.sync()
        item = self.s.store.items('handler')[0]
        execution = self.s.claim('handler', 'worker', item_id=item['id'])
        self.s.bind(execution['id'], execution['token'], str(self.wt), 'fix/mocx/bug')
        self.s.verify(execution['id'], execution['token'])
        self.s.report(execution['id'], execution['token'],
                      {'decision': 'APPROVE', 'reviewer': 'verifier', 'summary': 'done',
                       'action': 'create_pr', 'payload': {'title': 'fix', 'body': 'Closes #1',
                                                          'head': 'fix/mocx/bug', 'base': 'main'}})
        original_push = self.s.push
        self.s.push = lambda e, source: (_ for _ in ()).throw(ValueError('git down'))
        try:
            with self.assertRaisesRegex(ValueError, 'PUBLICATION_UNKNOWN'):
                self.s.publish(execution['id'], execution['token'])
        finally:
            self.s.push = original_push
        op = self.s.store.db.execute("SELECT id FROM outbox").fetchone()[0]
        self.assertEqual(self.outbox_state(op), 'UNKNOWN')
        self.assertEqual(self.platform.writes, [])
        # Conservative resume: the marker was never written, so it stays unknown.
        with self.assertRaisesRegex(ValueError, 'PUBLICATION_UNKNOWN'):
            self.s.publish(execution['id'], execution['token'])
        self.assertEqual(self.platform.writes, [])

    def test_pushed_false_records_unknown_without_platform_write(self):
        execution = self.issue_execution()
        prepared = self.s.prepare_publish(execution['id'], execution['token'])
        with self.assertRaisesRegex(ValueError, 'PUBLICATION_UNKNOWN'):
            self.s.complete_publish(execution['id'], execution['token'], prepared['op'],
                                    pushed=False)
        self.assertEqual(self.outbox_state(prepared['op']), 'UNKNOWN')
        self.assertEqual(self.platform.writes, [])

    def test_bind_checked_takes_worker_context(self):
        self.platform.data['prs'] = [{'number': 2, 'state': 'open', 'title': 'fix',
                                      'body': 'Closes #1', 'head': {'sha': git(self.wt, 'rev-parse', 'HEAD'),
                                                                    'ref': 'fix/mocx/bug'},
                                      'base': {'sha': 'base1', 'ref': 'main'}, 'merged': False,
                                      'mergeable': True, 'draft': False, 'comments': [],
                                      'reviews': [], 'checks': []}]
        self.s.sync()
        item = [x for x in self.s.store.items('reviewer') if x['state'] == 'READY'][0]
        execution = self.s.claim('reviewer', 'worker', item_id=item['id'])
        context = validate_worktree(self.cfg, str(self.wt), 'fix/mocx/bug')
        self.s.bind_checked(execution['id'], execution['token'], context)
        row = self.s.store.db.execute('SELECT branch FROM executions WHERE id=?',
                                      (execution['id'],)).fetchone()
        self.assertEqual(row['branch'], 'fix/mocx/bug')

    def test_record_verification_takes_worker_context(self):
        self.platform.data['prs'] = [{'number': 2, 'state': 'open', 'title': 'fix',
                                      'body': 'Closes #1', 'head': {'sha': git(self.wt, 'rev-parse', 'HEAD'),
                                                                    'ref': 'fix/mocx/bug'},
                                      'base': {'sha': 'base1', 'ref': 'main'}, 'merged': False,
                                      'mergeable': True, 'draft': False, 'comments': [],
                                      'reviews': [], 'checks': []}]
        self.s.sync()
        item = [x for x in self.s.store.items('reviewer') if x['state'] == 'READY'][0]
        execution = self.s.claim('reviewer', 'worker', item_id=item['id'])
        self.s.bind_checked(execution['id'], execution['token'],
                            validate_worktree(self.cfg, str(self.wt), 'fix/mocx/bug'))
        context = validate_worktree(self.cfg, str(self.wt), 'fix/mocx/bug')
        checks = run_checks(self.cfg, context,
                            Path(self.cfg['state_dir']) / 'executions' / execution['id'] / '1')
        result = self.s.record_verification(execution['id'], execution['token'], context, checks)
        self.assertTrue(result['passed'])
        self.assertEqual(result['input_version'], execution['item_version'])
        # A stale head cannot ride through the worker-supplied context.
        stale = dict(context, head='deadbeef')
        checks2 = run_checks(self.cfg, context,
                             Path(self.cfg['state_dir']) / 'executions' / execution['id'] / '2')
        with self.assertRaisesRegex(ValueError, 'PR_HEAD_MISMATCH'):
            self.s.record_verification(execution['id'], execution['token'], stale, checks2)

    def test_service_level_operator_verbs_wrap_store(self):
        # reconcile lives on the Service surface so the CLI has one dispatch;
        # it still demands the machine enablement marker first. recover was
        # removed entirely: derivation plus reconcile cover every ending.
        self.assertFalse(hasattr(self.s, 'recover'))
        self.s.disable()
        with self.assertRaisesRegex(ValueError, 'MACHINE_NOT_ENABLED'):
            self.s.reconcile('op', {'receipt': 1})
        self.s.enable()
        with self.assertRaisesRegex(ValueError, 'REMOTE_RECEIPT_REQUIRED'):
            self.s.reconcile('missing', {})

    def test_gate_without_context_keeps_lazy_validation_order(self):
        merged = {'number': 2, 'state': 'closed', 'title': 'fix', 'body': 'Closes #1',
                  'head': {'sha': 'h', 'ref': 'fix/mocx/bug'},
                  'base': {'sha': 'base1', 'ref': 'main'}, 'merged': True,
                  'mergeable': True, 'draft': False, 'comments': [], 'reviews': [], 'checks': []}
        self.platform.data['issues'] = [self.issue()]
        self.platform.data['prs'] = [merged]
        self.s.sync()
        item = self.s.store.items('handler')[0]
        execution = self.s.claim('handler', 'worker', item_id=item['id'])
        self.s.report(execution['id'], execution['token'],
                      {'decision': 'RESOLVED', 'reviewer': 'independent', 'summary': 'm',
                       'action': 'close_issue', 'payload': {'number': 1}})
        # No bind: gate must raise the worktree error, not a git error, without context.
        with self.assertRaisesRegex(ValueError, 'POST_MERGE_WORKTREE_REQUIRED'):
            self.s.prepare_publish(execution['id'], execution['token'])


if __name__ == '__main__':
    unittest.main()
