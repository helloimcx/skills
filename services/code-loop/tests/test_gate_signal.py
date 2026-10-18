#!/usr/bin/env python3
"""Gate-signal trust: flake rerun, base attribution, gate ledger, terminal reuse.

Covers the exit edges of "cannot judge": a red that survives the rerun is
attributed against base (stale-branch -> sync-base repair; base-defect ->
discover lane + freeze until main advances), an env failure becomes a gate
report that mints a GATE_REPAIR item (with escalation after N failed
repairs), and terminal review verdicts are never re-litigated.
"""
import copy
import json
import os
import sqlite3
import subprocess
import sys as _sys
import sys
import tempfile
import unittest
from pathlib import Path

for _p in (str(Path(__file__).resolve().parents[1]),
           str(Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.cp.runtime import (_attrib_decision, run_checks, run_gate_commands,
                                 validate_worktree)
from codeloop.cp.service import Service, gate_key


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args],
                                   stderr=subprocess.DEVNULL, text=True).strip()


class Platform:
    def __init__(self):
        self.data = {'base_sha': 'base1', 'issues': [], 'prs': []}
        self.writes = []
    def snapshot(self, known=None):
        return copy.deepcopy(self.data)
    def raw_file(self, path, ref):
        # No repository exam in this fixture: the machine config stands.
        return None
    def find(self, marker, action, payload):
        return next((r for a, p, r in self.writes if marker in p.get('body', '')), None)
    def write(self, action, payload):
        receipt = {'number': 100 + len(self.writes)}
        self.writes.append((action, payload, receipt))
        return receipt


class AttributionTreeTests(unittest.TestCase):
    def test_stale_branch_requires_base_tip_green(self):
        self.assertEqual(_attrib_decision(False, True), 'stale-branch')
    def test_base_defect_when_tip_also_red(self):
        self.assertEqual(_attrib_decision(False, False), 'base-defect')
    def test_pr_introduced_when_merge_base_green(self):
        self.assertEqual(_attrib_decision(True, False), 'pr-introduced')
        self.assertEqual(_attrib_decision(True, True), 'pr-introduced')


class RunGateCommandsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {'gate_command_allow': [sys.executable, 'make']}
    def tearDown(self):
        self.tmp.cleanup()
    def test_green_commands_pass(self):
        result = run_gate_commands(self.cfg, [[sys.executable, '-c', 'pass']],
                                   self.tmp.name, Path(self.tmp.name) / 'ev')
        self.assertTrue(result['passed'])
    def test_failing_command_is_a_code_failure(self):
        result = run_gate_commands(self.cfg, [[sys.executable, '-c', 'import sys; sys.exit(3)']],
                                   self.tmp.name, Path(self.tmp.name) / 'ev')
        self.assertFalse(result['passed'])
        self.assertEqual(result['checks'][0]['failure_class'], 'code')
    def test_non_argv_commands_rejected(self):
        with self.assertRaisesRegex(ValueError, 'GATE_COMMANDS_MUST_BE_ARGV'):
            run_gate_commands(self.cfg, ['make build'], self.tmp.name, Path(self.tmp.name) / 'ev')
    def test_missing_cwd_rejected(self):
        with self.assertRaisesRegex(ValueError, 'GATE_CWD_MISSING'):
            run_gate_commands(self.cfg, [[sys.executable, '-c', 'pass']],
                              Path(self.tmp.name) / 'nope', Path(self.tmp.name) / 'ev')
    def test_executable_must_be_allowlisted(self):
        with self.assertRaisesRegex(ValueError, 'GATE_COMMAND_NOT_ALLOWED'):
            run_gate_commands(self.cfg, [['powershell', '-c', 'echo hi']],
                              self.tmp.name, Path(self.tmp.name) / 'ev')
    def test_missing_allowlist_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'GATE_COMMAND_ALLOWLIST_REQUIRED'):
            run_gate_commands({}, [[sys.executable, '-c', 'pass']],
                              self.tmp.name, Path(self.tmp.name) / 'ev')
    def test_dotdot_cannot_ride_a_directory_entry(self):
        """A raw-prefix compare would admit <dir>/../../evil; the match is on
        the resolved path, so traversal stays outside."""
        exe_parent = str(Path(sys.executable).resolve().parent)
        cfg = {'gate_command_allow': [exe_parent]}
        escape = str(Path(exe_parent) / '..' / 'evil.exe')
        with self.assertRaisesRegex(ValueError, 'GATE_COMMAND_NOT_ALLOWED'):
            run_gate_commands(cfg, [[escape, '-c', 'pass']],
                              self.tmp.name, Path(self.tmp.name) / 'ev')
    def test_directory_entry_admits_its_own_children(self):
        cfg = {'gate_command_allow': [str(Path(sys.executable).resolve().parent)]}
        result = run_gate_commands(cfg, [[sys.executable, '-c', 'pass']],
                                   self.tmp.name, Path(self.tmp.name) / 'ev')
        self.assertTrue(result['passed'])


class GateSignalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._home = os.environ.get('CODE_LOOP_HOME')
        os.environ['CODE_LOOP_HOME'] = self.tmp.name
        # A real git repo backs the gate-repair checkout (bind attests its
        # head with a real rev-parse).
        repo = Path(self.tmp.name) / 'repo'
        repo.mkdir()
        git(repo, 'init', '-b', 'main')
        git(repo, 'config', 'user.email', 't@invalid')
        git(repo, 'config', 'user.name', 'T')
        (repo / 'f.txt').write_text('x')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'init')
        self.repo_root = str(repo)
        self.cfg = {'state_dir': Path(self.tmp.name) / 'state', 'base': 'main',
                    'platform': {'repo': 'a/b'}, 'verification': [], 'required_checks': [],
                    'max_parallel': 3, 'lease_seconds': 1800, 'project_key': 'a__b',
                    'snapshot_ttl': 300, 'discovery_window_seconds': 10800,
                    'gate_command_allow': [sys.executable],
                    'repo_root': self.repo_root}
        self.platform = Platform()
        self.s = Service(self.cfg, self.platform)
        self.s.enable()

    def tearDown(self):
        self.s.store.db.close()
        self.tmp.cleanup()
        if self._home is None:
            os.environ.pop('CODE_LOOP_HOME', None)
        else:
            os.environ['CODE_LOOP_HOME'] = self._home

    def expire_observer(self):
        path = Path(os.environ['CODE_LOOP_HOME']) / 'observer.sqlite'
        if not path.is_file():
            return
        db = sqlite3.connect(str(path))
        try:
            db.execute("UPDATE meta SET value=? WHERE key='synced_at'", (json.dumps(0),))
            db.commit()
        except sqlite3.OperationalError:
            pass  # store not initialized yet — nothing to age
        finally:
            db.close()

    def sync(self, base_sha='base1', prs=(), issues=()):
        self.expire_observer()
        self.platform.data = {'base_sha': base_sha, 'issues': list(issues), 'prs': list(prs)}
        self.s.sync()

    def pr(self, number=2, sha='head1', mergeable=True):
        return {'number': number, 'state': 'open', 'title': 'fix', 'body': '',
                'head': {'sha': sha, 'ref': 'fix/mocx/x'},
                'base': {'sha': 'base1', 'ref': 'main'}, 'merged': False,
                'mergeable': mergeable, 'draft': False, 'comments': [], 'reviews': [], 'checks': []}

    def review_round(self, sha='head1', env_signature=None, attribution=None):
        """sync + claim + bind + record verification evidence for PR #2."""
        self.sync(prs=[self.pr(sha=sha)])
        e = self.s.claim('reviewer', 'w1')
        ctx = {'worktree': 'wt', 'branch': 'fix/mocx/x', 'head': sha}
        self.s.bind_checked(e['id'], e['token'], ctx)
        checks = dict(ctx, passed=False, checks=[])
        if env_signature:
            checks['env_signature'] = env_signature
        if attribution:
            checks['attribution'] = attribution
        self.s.record_verification(e['id'], e['token'], ctx, checks)
        return e

    def current_keys(self):
        return {i['key'] for i in self.s.store.items()}

    def ready_keys(self):
        # Only READY rows are live work: claimed RUNNING rows of superseded
        # versions linger until their lease expires and are not re-derived.
        return {i['key'] for i in self.s.store.items() if i['state'] == 'READY'}

    # -- Blocked is a gate report, never an unsupported verdict -------------
    def test_blocked_report_requires_gate_evidence(self):
        e = self.review_round(env_signature='no-module:x')
        base = {'decision': 'Blocked', 'reviewer': 'w2', 'summary': 'env broken'}
        with self.assertRaisesRegex(ValueError, 'GATE_REPORT_REQUIRED'):
            self.s.report(e['id'], e['token'], dict(base))
        with self.assertRaisesRegex(ValueError, 'GATE_SIGNATURE_MISMATCH'):
            self.s.report(e['id'], e['token'], dict(base, gate={'signature': 'other'}))
        with self.assertRaisesRegex(ValueError, 'INVALID_GATE_REPORT'):
            # env class without repair commands: an unrunnable repair would
            # burn three guaranteed-failed rounds before escalation.
            self.s.report(e['id'], e['token'],
                          dict(base, gate={'signature': 'no-module:x'}))
        result = self.s.report(e['id'], e['token'],
                               dict(base, gate={'signature': 'no-module:x',
                                                'commands': [['make', 'build_vector']]}))
        self.assertEqual(result['execution_state'], 'SUCCEEDED')
        entry = self.s.meta('gates')['no-module:x']
        self.assertEqual(entry['state'], 'open')
        self.assertEqual(entry['attempts'], 1)

    # -- gate open freezes the review and mints the repair item -------------
    def test_gate_freezes_review_and_mints_repair_item(self):
        e = self.review_round(env_signature='no-module:x')
        self.s.report(e['id'], e['token'], {'decision': 'Blocked', 'reviewer': 'w2',
                                            'summary': 'env', 'gate': {
                                                'signature': 'no-module:x',
                                                'commands': [['make', 'build_vector']],
                                                'note': 'cython not built'}})
        self.sync(prs=[self.pr()])
        self.assertNotIn('review:2', self.ready_keys())
        gate_item = next(i for i in self.s.store.items() if i['key'] == gate_key('no-module:x'))
        self.assertEqual(gate_item['state'], 'READY')
        self.assertEqual(gate_item['kind'], 'handler')
        self.assertEqual(json.loads(gate_item['resources']),
                         ['gate-' + gate_key('no-module:x').split(':')[1]])
        self.assertEqual(json.loads(gate_item['payload'])['commands'],
                         [['make', 'build_vector']])

    def test_gate_repair_resolves_and_rederives_review(self):
        e = self.review_round(env_signature='no-module:x')
        self.s.report(e['id'], e['token'], {'decision': 'Blocked', 'reviewer': 'w2',
                                            'summary': 'env', 'gate': {
                                                'signature': 'no-module:x',
                                                'commands': [[sys.executable, '-c', 'pass']],
                                                'note': ''}})
        self.sync(prs=[self.pr()])
        g = self.s.claim('handler', 'h1')
        self.assertEqual(g['item_id'],
                         next(i['id'] for i in self.s.store.items()
                              if i['key'] == gate_key('no-module:x')))
        self.s.bind(g['id'], g['token'], self.repo_root, None)
        result = self.s.verify(g['id'], g['token'])
        self.assertTrue(result['passed'])
        self.s.report(g['id'], g['token'], {'decision': 'RESOLVED', 'reviewer': 'w2',
                                            'summary': 'rebuilt'})
        self.assertEqual(self.s.meta('gates')['no-module:x']['state'], 'repaired')
        self.sync(prs=[self.pr()])
        # The review item re-derives under a NEW version carrying the gate
        # state — a same-version row would stay dead at SUCCEEDED forever.
        item = next(i for i in self.s.store.items()
                    if i['key'] == 'review:2' and i['state'] == 'READY')
        self.assertEqual(json.loads(item['payload'])['gate'],
                         {'signature': 'no-module:x', 'state': 'repaired'})

    def test_gate_repair_blocked_counts_failures_and_retries(self):
        e = self.review_round(env_signature='no-module:x')
        self.s.report(e['id'], e['token'], {'decision': 'Blocked', 'reviewer': 'w2',
                                            'summary': 'env', 'gate': {
                                                'signature': 'no-module:x',
                                                'commands': [[sys.executable, '-c',
                                                              'import sys; sys.exit(1)']],
                                                'note': ''}})
        self.sync(prs=[self.pr()])
        g = self.s.claim('handler', 'h1')
        self.s.bind(g['id'], g['token'], self.repo_root, None)
        self.assertFalse(self.s.verify(g['id'], g['token'])['passed'])
        self.s.report(g['id'], g['token'], {'decision': 'BLOCKED', 'reviewer': 'w2',
                                            'summary': 'build failed'})
        entry = self.s.meta('gates')['no-module:x']
        self.assertEqual(entry['repair_failures'], 1)
        self.assertEqual(entry['state'], 'open')
        self.sync(prs=[self.pr()])
        # The failed attempt re-mints the repair item under a new version.
        gate_rows = [i for i in self.s.store.items() if i['key'] == gate_key('no-module:x')]
        self.assertEqual(len(gate_rows), 2)
        self.assertTrue(any(i['state'] == 'READY' for i in gate_rows))

    def test_gate_escalation_after_max_attempts(self):
        self.s.set_meta('gates', {'no-module:x': {'state': 'open', 'attempts': 1,
                                                  'repair_failures': 3, 'note': 'n',
                                                  'commands': []}})
        self.sync()
        self.assertNotIn(gate_key('no-module:x'), self.current_keys())
        self.assertNotIn('review:2', self.current_keys())
        discover = next(i for i in self.s.store.items() if i['key'] == 'discover')
        escalations = json.loads(discover['payload'])['gate_escalations']
        self.assertEqual(escalations[0]['signature'], 'no-module:x')
        self.assertEqual(self.s.meta('gates')['no-module:x']['state'], 'escalated')

    # -- base attribution edges --------------------------------------------
    def test_stale_branch_attribution_derives_sync_base(self):
        # Signals are verdict-delivered facts: finish the round (Blocked) so
        # its red counts, then the derivation routes the PR to sync-base.
        e = self.review_round(attribution={'verdict': 'stale-branch', 'base_tip_sha': 'base1'})
        self.s.report(e['id'], e['token'], {'decision': 'Blocked', 'reviewer': 'w2',
                                            'summary': 'branch stale',
                                            'gate': {'note': 'stale-branch'}})
        self.sync(prs=[self.pr()])
        self.assertNotIn('review:2', self.ready_keys())
        repair = next(i for i in self.s.store.items() if i['key'] == 'repair:2')
        self.assertEqual(json.loads(repair['payload'])['task'], 'PR_SYNC_BASE')

    def test_attribution_blocked_report_validated_and_ledger_free(self):
        """Base attribution exits through sync-base / the discover lane: the
        report is accepted, carries no commands, and registers no gate."""
        e = self.review_round(attribution={'verdict': 'stale-branch', 'base_tip_sha': 'base1'})
        base = {'decision': 'Blocked', 'reviewer': 'w2', 'summary': 'branch is stale'}
        with self.assertRaisesRegex(ValueError, 'INVALID_GATE_REPORT'):
            self.s.report(e['id'], e['token'],
                          dict(base, gate={'commands': [['make', 'build']]}))
        result = self.s.report(e['id'], e['token'], dict(base, gate={'note': 'stale-branch'}))
        self.assertEqual(result['execution_state'], 'SUCCEEDED')
        self.assertFalse(self.s.meta('gates'))

    def test_base_defect_freezes_until_base_advances(self):
        # Finish the round so the red is a verdict-delivered fact (signal),
        # then the freeze holds while the base tip stays at the red commit.
        e = self.review_round(attribution={'verdict': 'base-defect', 'base_tip_sha': 'tip1'})
        self.s.report(e['id'], e['token'], {'decision': 'Blocked', 'reviewer': 'w2',
                                            'summary': 'base red',
                                            'gate': {'note': 'base-defect'}})
        self.sync(base_sha='tip1', prs=[self.pr()])
        self.assertNotIn('review:2', self.ready_keys())
        self.sync(base_sha='tip2', prs=[self.pr()])
        self.assertIn('review:2', self.ready_keys())

    # -- terminal verdicts are facts, not reopeners -------------------------
    def _sent_verdict(self, eid, decision):
        self.s.store.db.execute('INSERT INTO outbox VALUES (?,?,?,?,?,NULL)',
                                ('op-' + decision, eid, 'review_comment',
                                 json.dumps({'number': 2, 'body': 'Decision: ' + decision
                                             + '\nHead: head1'}, ensure_ascii=False),
                                 'SENT'))

    def test_validation_failed_comment_derives_repair(self):
        e = self.review_round()
        self._sent_verdict(e['id'], 'Validation failed')
        self.sync(prs=[self.pr()])
        repair = next(i for i in self.s.store.items() if i['key'] == 'repair:2')
        self.assertEqual(json.loads(repair['payload'])['task'], 'PR_REVIEW_FIX')

    def test_approved_comment_not_rederived(self):
        e = self.review_round()
        self._sent_verdict(e['id'], 'Approved for manual merge')
        self.sync(prs=[self.pr()])
        keys = self.ready_keys()
        self.assertNotIn('review:2', keys)
        self.assertNotIn('repair:2', keys)


class RerunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-b', 'main')
        git(self.repo, 'config', 'user.email', 't@invalid')
        git(self.repo, 'config', 'user.name', 'T')
        (self.repo / 'check.py').write_text('raise SystemExit(1)\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'initial')
        self.wt = self.root / 'trees' / 'issue-1'
        git(self.repo, 'worktree', 'add', '-b', 'feature/mocx/test', str(self.wt))
        self.cfg = {'repo_root': str(self.repo), 'worktree_root': str(self.root / 'trees'),
                    'branch_prefixes': ['feature/mocx/'], 'base': 'main',
                    'verification': [[sys.executable, '{worktree}/check.py']],
                    'verify_timeout': 30}
        self.context = validate_worktree(self.cfg, str(self.wt), 'feature/mocx/test')

    def tearDown(self):
        self.tmp.cleanup()

    def _flip_after_first(self, real_run):
        # First check.py run flakes red, the rerun runs green; everything
        # else (git plumbing) goes to the real subprocess.
        state = {'n': 0}
        def runner(argv, **kwargs):
            if any(str(a).endswith('check.py') for a in argv):
                state['n'] += 1
                class R:
                    returncode = 0 if state['n'] >= 2 else 1
                return R()
            return real_run(argv, **kwargs)
        return runner

    def test_rerun_washes_out_a_flake(self):
        import unittest.mock as mock
        with mock.patch.object(subprocess, 'run',
                               side_effect=self._flip_after_first(subprocess.run)):
            result = run_checks(self.cfg, self.context, self.root / 'ev')
        self.assertTrue(result['passed'])
        check = result['checks'][0]
        self.assertEqual(check['exit_code'], 0)
        self.assertEqual(check['first_run']['exit_code'], 1)
        self.assertTrue((self.root / 'ev' / 'check-0-rerun.log').exists())

    def test_surviving_red_gets_attributed_end_to_end(self):
        """C1 regression: a real red with extractable nodes must reach
        attribution without crashing (evidence dirs, origin fetch, throwaway
        worktrees all inside run_checks)."""
        origin = self.root / 'origin.git'
        git(self.repo, 'init', '--bare', '-b', 'main', str(origin))
        git(self.repo, 'remote', 'add', 'origin', str(origin))
        git(self.repo, 'push', 'origin', 'main')
        (self.repo / 'check.py').write_text(
            'import sys\nprint("FAILED tests/test_a.py::test_one - boom")\nsys.exit(1)\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'red check')
        git(self.repo, 'push', 'origin', 'main')
        # The task branch must carry the red check for node extraction.
        git(self.wt, 'merge', 'main')
        wt_head = git(self.wt, 'rev-parse', 'HEAD')
        cfg = dict(self.cfg, verification=[[sys.executable, '{worktree}/check.py']])
        result = run_checks(cfg, dict(self.context, head=wt_head), self.root / 'ev')
        self.assertFalse(result['passed'])
        attribution = result.get('attribution')
        self.assertIsNotNone(attribution)
        self.assertEqual(attribution['verdict'], 'base-defect')
        self.assertEqual(attribution['nodes'], ['tests/test_a.py::test_one'])
        attrib_dir = self.root / 'trees' / '_attrib'
        self.assertFalse(attrib_dir.exists() and any(attrib_dir.iterdir()),
                         'no throwaway checkout may survive attribution')

    def test_rerun_disabled_keeps_legacy_behavior(self):
        import unittest.mock as mock
        cfg = dict(self.cfg, verify_rerun=False)
        with mock.patch.object(subprocess, 'run',
                               side_effect=self._flip_after_first(subprocess.run)):
            result = run_checks(cfg, self.context, self.root / 'ev')
        self.assertFalse(result['passed'])
        self.assertNotIn('first_run', result['checks'][0])


if __name__ == '__main__':
    unittest.main()
