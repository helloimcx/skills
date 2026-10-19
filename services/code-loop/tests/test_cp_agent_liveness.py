#!/usr/bin/env python3
"""Spawn liveness: the grace window and the silent-death watcher (issue #229).

Split from test_cp_agent.py (issue #185 convention: split, don't raise) --
PR #231 brought the class in and pushed that file over the 800-line red
line; the suite is unchanged, only relocated."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import codeloop.agent as agent_mod
import codeloop.agent_watch as agent_watch_mod
from codeloop import trigger
from test_cp_agent import EXEC_ID, FakeClient, FakeGit, ScriptedWorker, registration


class SpawnLivenessTests(unittest.TestCase):
    """A process that dies in the first seconds must not sit on the ledger as
    a live round: the spawn is watched briefly, a dead one is acked FAILED
    with its exit code and carries the claim (execution+token) so the server
    can release it — no phantom '审核中' card, no worktree held for hours."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'repo').mkdir()
        (self.root / 'entry.md').write_text('entry', encoding='utf-8')
        (self.root / 'rules.md').write_text('rules', encoding='utf-8')
        self.cli = self.root / 'claude.exe'
        self.spawned = []
        self.followed = []
        # the production window is seconds; tests must not sleep it
        self.grace = 0.02
        self.client = FakeClient()
        self.row = registration(self.root, self.cli)
        self.worker = ScriptedWorker(claim_result=self.claim_result())
        self.agent = agent_mod.WorkerAgent(
            server_url='http://cp.example', token='tok',
            popen_fn=self.popen, client=self.client,
            git_fn=FakeGit(),
            registration_loader=lambda project: self.row,
            cfg_factory=lambda project, row: {'repo_root': str(self.root / 'repo'),
                                              'worktree_root': str(self.root / 'wt')},
            worker_factory=lambda cfg: self.worker,
            spawn_argv_fn=lambda agent, model: None,
            spawn_grace_seconds=0.02,
            spawn_grace_poll_seconds=0.001,
            follow_fn=lambda *a, **k: self.followed.append((a, k)))

    def tearDown(self):
        self.tmp.cleanup()

    def popen(self, argv, cwd=None, stdin=None, stdout=None, stderr=None, **kw):
        self.spawned.append({'argv': argv, 'cwd': cwd, 'log': stdout.name})
        return self.proc()

    def proc(self, rc=None, with_poll=True):
        """A process double shaped like a real Popen: ``poll()`` is what the
        liveness window reads, and its absence means 'not observable'."""
        fields = {'pid': 4242}
        if with_poll:
            fields['poll'] = lambda _self: rc
        return type('Proc', (), fields)()

    @staticmethod
    def claim_result():
        return {'execution': {'id': EXEC_ID, 'token': 3, 'item_id': 'item-1'},
                'item': {'id': 'item-1', 'payload': json.dumps({'pr': {
                    'number': 88, 'title': 'PR 88',
                    'head': {'ref': 'feature/x', 'sha': 'abc'}}})}}

    def ack(self):
        return [c for c in self.client.calls if c[1].endswith('/command_ack')]

    def command(self, **overrides):
        base = {'id': 1, 'action': 'review_round', 'number': 88, 'project': 'a__b'}
        base.update(overrides)
        return base

    def round_log(self):
        return self.root / 'state' / 'executions' / (EXEC_ID + '.log')

    def test_process_dying_in_the_grace_window_is_acked_failed(self):
        self.agent.popen_fn = lambda argv, **kw: self.proc(rc=1)
        self.agent.execute(self.command())
        self.assertEqual(1, len(self.ack()))
        body = self.ack()[0][2]
        self.assertEqual('FAILED', body['outcome'])
        self.assertIn('SPAWN_EXIT', body['fail_reason'])
        self.assertIn('rc=1', body['fail_reason'])
        # the claim rides along so the server can hand the item back
        self.assertEqual(EXEC_ID, body['execution'])
        self.assertEqual(3, body['token'])
        self.assertEqual([], self.followed)   # nothing to follow

    def test_surviving_process_is_acked_spawned(self):
        self.agent.execute(self.command())
        body = self.ack()[0][2]
        self.assertEqual('SPAWNED', body['outcome'])
        self.assertEqual(EXEC_ID, body['execution'])
        self.assertIsNone(body['fail_reason'])
        self.assertEqual(1, len(self.followed))

    def test_clean_exit_inside_the_window_is_not_a_startup_failure(self):
        """A zero exit means the session ran and finished — a short round, not
        a process that could not start. Only a non-zero exit is a failure to
        run; how a round completes is the control plane's business."""
        self.agent.popen_fn = lambda argv, **kw: self.proc(rc=0)
        self.agent.execute(self.command())
        self.assertEqual('SPAWNED', self.ack()[0][2]['outcome'])

    def test_process_without_a_poll_hook_is_never_judged_dead(self):
        """Exotic popen wrappers and test doubles are unobservable, not dead:
        guessing here would fail healthy rounds."""
        self.agent.popen_fn = lambda argv, **kw: self.proc(with_poll=False)
        self.agent.execute(self.command())
        self.assertEqual('SPAWNED', self.ack()[0][2]['outcome'])

    def test_bookkeeping_failure_never_fails_a_live_round(self):
        """The diagnostic write is best effort: a live round is never failed —
        let alone handed back — because its log line could not be written. A
        real unwritable log path (the file replaced by a directory) is the
        trigger, not a stub."""
        self.agent.spawn_grace_seconds = 0
        state = self.root / 'state'
        state.mkdir(exist_ok=True)
        (state / trigger.SPAWN_LOG.replace('spawn', 'log')).mkdir()
        self.agent.execute(self.command())
        body = self.ack()[0][2]
        self.assertEqual('SPAWNED', body['outcome'])
        self.assertEqual(EXEC_ID, body['execution'])
        self.assertNotIn('token', body)          # nothing was handed back
        self.assertEqual(1, len(self.followed))

    def test_window_boundary_is_a_poll_count_not_a_wall_clock(self):
        """An exit seen on the last poll of the window is still caught, and a
        process that outlives the window is not judged dead."""
        self.agent.spawn_grace_seconds = 0.005
        self.agent.spawn_grace_poll_seconds = 0.001          # five polls
        codes = [None, None, None, 3]
        late = type('P', (), {'pid': 1,
                              'poll': lambda _s: codes.pop(0) if codes else None})()
        self.assertEqual(3, self.agent._died_within_grace(late, 0.005))
        alive = type('P', (), {'pid': 1, 'poll': lambda _s: None})()
        self.assertIsNone(self.agent._died_within_grace(alive, 0.005))

    def test_spawned_ack_never_carries_a_claim_to_release(self):
        self.agent.spawn_grace_seconds = 0
        self.agent.execute(self.command())
        body = self.ack()[0][2]
        self.assertEqual('SPAWNED', body['outcome'])
        self.assertNotIn('token', body)

    def test_grace_window_can_be_disabled(self):
        self.agent.spawn_grace_seconds = 0
        self.agent.popen_fn = lambda argv, **kw: self.proc(rc=9)
        self.agent.execute(self.command())
        self.assertEqual('SPAWNED', self.ack()[0][2]['outcome'])

    def test_silent_death_after_grace_is_acked_failed_with_the_claim(self):
        """issue #229: 宽限窗口后静默死亡的会话必须 FAILED 回交 claim。"""
        from unittest import mock
        self.agent.spawn_grace_seconds = 0
        self.agent.execute(self.command())
        self.assertEqual('SPAWNED', self.ack()[0][2]['outcome'])
        claim = {'execution': EXEC_ID, 'token': 3}
        # pid 已消失：探测一次 + 确认窗口后 FAILED，claim 随行回交
        with mock.patch.object(agent_watch_mod, '_pid_alive', return_value=False), \
             mock.patch.object(agent_mod.time, 'sleep', return_value=None):
            self.agent._watch_process('a__b', 1, claim, 4242)
        failed = [c for c in self.client.calls
                  if c[1].endswith('/command_ack') and c[2].get('outcome') == 'FAILED']
        self.assertEqual(1, len(failed))
        body = failed[0][2]
        self.assertIn('PROCESS_DIED', body['fail_reason'])
        self.assertEqual(EXEC_ID, body['execution'])
        self.assertEqual(3, body['token'])
        # pid 存活：持续轮询，死亡后才 FAILED
        self.client.calls.clear()
        with mock.patch.object(agent_watch_mod, '_pid_alive', side_effect=[True, True, False]), \
             mock.patch.object(agent_mod.time, 'sleep', return_value=None):
            self.agent._watch_process('a__b', 1, claim, 4242)
        failed = [c for c in self.client.calls
                  if c[1].endswith('/command_ack') and c[2].get('outcome') == 'FAILED']
        self.assertEqual(1, len(failed))
        self.assertEqual(3, failed[0][2]['token'])

    def test_death_ack_carries_exit_code_and_log_tail(self):
        """The FAILED ack is the only record a silent death leaves: it must
        name the exit code and quote the round log's tail so the cause is
        attributable (03d8c5c7 postmortem: died mid-round, nothing but
        PROCESS_DIED survived)."""
        log = self.round_log()
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('warning: sandbox disabled\n\nfinal text line\n',
                       encoding='utf-8')
        claim = {'execution': EXEC_ID, 'token': 3}
        with mock.patch.object(agent_watch_mod, '_pid_alive', return_value=False), \
             mock.patch.object(agent_mod.time, 'sleep', return_value=None):
            self.agent._watch_process('a__b', 1, claim, 4242,
                                      proc=self.proc(rc=1), log_path=str(log))
        body = [c for c in self.client.calls
                if c[1].endswith('/command_ack') and c[2].get('outcome') == 'FAILED'
                ][0][2]
        self.assertIn('rc=1', body['fail_reason'])
        self.assertIn('log tail:', body['fail_reason'])
        self.assertIn('final text line', body['fail_reason'])
        self.assertLessEqual(len(body['fail_reason']), 800)

    def test_death_ack_without_proc_or_log_stays_actionable(self):
        claim = {'execution': EXEC_ID, 'token': 3}
        with mock.patch.object(agent_watch_mod, '_pid_alive', return_value=False), \
             mock.patch.object(agent_mod.time, 'sleep', return_value=None):
            self.agent._watch_process('a__b', 1, claim, 4242)
        body = [c for c in self.client.calls
                if c[1].endswith('/command_ack') and c[2].get('outcome') == 'FAILED'
                ][0][2]
        self.assertIn('PROCESS_DIED', body['fail_reason'])
        self.assertIn('rc=unknown', body['fail_reason'])
        self.assertNotIn('log tail', body['fail_reason'])

    def test_death_ack_with_unreadable_log_has_no_tail(self):
        log = self.root / 'gone' / 'missing.log'   # parent dir absent → OSError
        claim = {'execution': EXEC_ID, 'token': 3}
        with mock.patch.object(agent_watch_mod, '_pid_alive', return_value=False), \
             mock.patch.object(agent_mod.time, 'sleep', return_value=None):
            self.agent._watch_process('a__b', 1, claim, 4242,
                                      proc=self.proc(rc=3), log_path=str(log))
        body = [c for c in self.client.calls
                if c[1].endswith('/command_ack') and c[2].get('outcome') == 'FAILED'
                ][0][2]
        self.assertEqual('PROCESS_DIED: rc=3', body['fail_reason'])

    def test_death_reason_is_capped_and_keeps_its_head(self):
        log = self.round_log()
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('\n'.join('x' * 300 for _ in range(6)),
                       encoding='utf-8')
        claim = {'execution': EXEC_ID, 'token': 3}
        with mock.patch.object(agent_watch_mod, '_pid_alive', return_value=False), \
             mock.patch.object(agent_mod.time, 'sleep', return_value=None):
            self.agent._watch_process('a__b', 1, claim, 4242,
                                      proc=self.proc(rc=1), log_path=str(log))
        body = [c for c in self.client.calls
                if c[1].endswith('/command_ack') and c[2].get('outcome') == 'FAILED'
                ][0][2]
        self.assertLessEqual(len(body['fail_reason']), 800)
        self.assertTrue(body['fail_reason'].startswith('PROCESS_DIED: rc=1'))

    def test_round_output_lands_in_a_per_execution_log(self):
        self.agent.execute(self.command())
        self.assertEqual(str(self.round_log()), self.spawned[0]['log'])
        self.assertTrue(self.round_log().is_file())

    def test_spawn_failure_after_the_claim_carries_it_for_release(self):
        def refusing(argv, **kw):
            raise OSError('no such executable')
        self.agent.popen_fn = refusing
        self.agent.execute(self.command())
        body = self.ack()[0][2]
        self.assertEqual('FAILED', body['outcome'])
        self.assertIn('SPAWN_FAILED', body['fail_reason'])
        self.assertEqual((EXEC_ID, 3), (body['execution'], body['token']))

    def test_opencode_round_follows_its_own_log_not_a_transcript(self):
        self.agent.spawn_argv_fn = lambda agent, model: [str(self.cli), 'run']
        self.agent.execute(self.command(agent='opencode',
                                        model='kimi-for-coding/k3'))
        args, kwargs = self.followed[0]
        self.assertEqual(str(self.round_log()), kwargs['stream_path'])
        self.assertEqual(EXEC_ID, args[1])

    def test_unkeyable_execution_id_never_streams_the_shared_log(self):
        """The per-round log needs a keyable id; without one the shared cron
        log is NOT adopted as this round's stream (its contents belong to
        whatever ran last)."""
        self.agent.spawn_argv_fn = lambda agent, model: [str(self.cli), 'run']
        self.worker = ScriptedWorker(claim_result={
            'execution': {'id': 'exec-1', 'token': 3, 'item_id': 'item-1'},
            'item': {'id': 'item-1', 'payload': json.dumps({'pr': {
                'number': 88, 'title': 'PR 88',
                'head': {'ref': 'feature/x', 'sha': 'abc'}}})}})
        self.agent.execute(self.command(agent='opencode',
                                        model='kimi-for-coding/k3'))
        _, kwargs = self.followed[0]
        self.assertIsNone(kwargs['stream_path'])
        self.assertTrue(self.spawned[0]['log'].endswith('trigger-spawn.log'))

    def test_claude_round_keeps_binding_the_transcript(self):
        self.agent.execute(self.command())
        _, kwargs = self.followed[0]
        self.assertIsNone(kwargs['stream_path'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
