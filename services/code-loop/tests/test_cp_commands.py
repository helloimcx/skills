#!/usr/bin/env python3
"""Command ledger: the operator-instruction state machine and the dispatcher
that hands pending commands to live WS agents. Store semantics run against a
real sqlite; the dispatcher runs against a real coordinator (service_factory)
with scripted connections."""
import json
import os
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

from codeloop.cp import server
from codeloop.cp.protocol import ProtocolError
from codeloop.cp.server import Coordinator, CommandDispatcher
from codeloop.cp.service import Service
from codeloop.cp.store import Store


def make_store(tmp):
    return Store(str(Path(tmp) / 'cp.sqlite'))


class CommandStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self.tmp.name)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_enqueue_is_idempotent_while_live(self):
        row, created = self.store.enqueue_command('review_round', 88, now=1000.0)
        self.assertTrue(created)
        again, created = self.store.enqueue_command('review_round', 88, now=1001.0)
        self.assertFalse(created)
        self.assertEqual(row['id'], again['id'])
        # a terminal command does not block a fresh one
        self.store.mark_dispatched(row['id'], 'm1', now=1002.0)
        self.store.ack_command(row['id'], 'm1', 'FAILED', fail_reason='CLAIM_UNAVAILABLE')
        fresh, created = self.store.enqueue_command('review_round', 88, now=1003.0)
        self.assertTrue(created)
        self.assertEqual('PENDING', fresh['state'])

    def test_legacy_commands_table_gains_the_execution_column(self):
        """A store created before the execution column (this branch's own
        intermediate shape) is migrated on the next connect — no manual step,
        no dropped data."""
        state = Path(self.tmp.name) / 'legacy-state'
        import sqlite3
        state.mkdir()
        conn = sqlite3.connect(str(state / 'control-plane.sqlite'))
        conn.execute('CREATE TABLE commands ('
                     'id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,'
                     ' number INTEGER NOT NULL, state TEXT NOT NULL,'
                     ' target_machine TEXT, claimed_by TEXT, claimed_at REAL,'
                     ' spawned_at REAL, attempts INTEGER NOT NULL DEFAULT 0,'
                     ' fail_reason TEXT, created_at REAL NOT NULL)')
        conn.execute("INSERT INTO commands(action,number,state,created_at) "
                     "VALUES ('review_round',88,'PENDING',1000.0)")
        conn.commit()
        conn.close()
        migrated = Store(str(state))
        try:
            columns = {r[1] for r in migrated.db.execute('PRAGMA table_info(commands)')}
            self.assertIn('execution', columns)
            self.assertEqual(88, migrated.latest_command('review_round', 88)['number'])
        finally:
            migrated.db.close()

    def test_legacy_commands_table_gains_agent_model_columns(self):
        """Production stores predate the agent/model spawn-option columns; the
        next connect migrates them in without touching existing rows."""
        state = Path(self.tmp.name) / 'legacy-state-2'
        import sqlite3
        state.mkdir()
        conn = sqlite3.connect(str(state / 'control-plane.sqlite'))
        conn.execute('CREATE TABLE commands ('
                     'id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,'
                     ' number INTEGER NOT NULL, state TEXT NOT NULL,'
                     ' target_machine TEXT, claimed_by TEXT, claimed_at REAL,'
                     ' spawned_at REAL, attempts INTEGER NOT NULL DEFAULT 0,'
                     ' fail_reason TEXT, execution TEXT, created_at REAL NOT NULL)')
        conn.execute("INSERT INTO commands(action,number,state,created_at) "
                     "VALUES ('review_round',88,'PENDING',1000.0)")
        conn.commit()
        conn.close()
        migrated = Store(str(state))
        try:
            columns = {r[1] for r in migrated.db.execute('PRAGMA table_info(commands)')}
            self.assertIn('agent', columns)
            self.assertIn('model', columns)
            row = migrated.latest_command('review_round', 88)
            self.assertIsNone(row['agent'])
            self.assertIsNone(row['model'])
        finally:
            migrated.db.close()

    def test_enqueue_persists_agent_and_model(self):
        row, created = self.store.enqueue_command(
            'review_round', 88, now=1000.0, agent='opencode',
            model='kimi-for-coding/k3')
        self.assertTrue(created)
        self.assertEqual(('opencode', 'kimi-for-coding/k3'),
                         (row['agent'], row['model']))
        self.assertEqual(('opencode', 'kimi-for-coding/k3'),
                         (self.store.latest_command('review_round', 88)['agent'],
                          self.store.latest_command('review_round', 88)['model']))

    def test_enqueue_without_options_keeps_them_null(self):
        row, _ = self.store.enqueue_command('review_round', 88, now=1000.0)
        self.assertIsNone(row['agent'])
        self.assertIsNone(row['model'])

    def test_mark_dispatched_is_single_winner(self):
        row, _ = self.store.enqueue_command('review_round', 88, now=1000.0)
        first = self.store.mark_dispatched(row['id'], 'm1', now=1001.0)
        second = self.store.mark_dispatched(row['id'], 'm2', now=1002.0)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual('m1', first['claimed_by'])
        self.assertEqual(1, first['attempts'])

    def test_ack_requires_the_claiming_machine(self):
        row, _ = self.store.enqueue_command('review_round', 88, now=1000.0)
        self.store.mark_dispatched(row['id'], 'm1', now=1001.0)
        self.assertIsNone(self.store.ack_command(row['id'], 'm2', 'SPAWNED'))
        acked = self.store.ack_command(row['id'], 'm1', 'SPAWNED', now=1002.0)
        self.assertEqual('SPAWNED', acked['state'])
        self.assertIsNotNone(acked['spawned_at'])
        self.assertIsNone(self.store.ack_command(row['id'], 'm1', 'SPAWNED'))

    def test_ack_failed_carries_the_reason(self):
        row, _ = self.store.enqueue_command('review_round', 88, now=1000.0)
        self.store.mark_dispatched(row['id'], 'm1', now=1001.0)
        acked = self.store.ack_command(row['id'], 'm1', 'FAILED',
                                       fail_reason='CLAIM_UNAVAILABLE', now=1002.0)
        self.assertEqual(('FAILED', 'CLAIM_UNAVAILABLE'),
                         (acked['state'], acked['fail_reason']))

    def test_sweep_expires_stale_pending(self):
        row, _ = self.store.enqueue_command('review_round', 88, now=1000.0)
        counts = self.store.sweep_commands(now=1000.0 + 1800, ttl=1800)
        self.assertEqual(1, counts['expired'])
        self.assertEqual('EXPIRED', self.store.latest_command('review_round', 88)['state'])

    def test_spawned_closes_once_its_execution_is_terminal(self):
        """SPAWNED needs an exit edge, or one click would disable the card
        forever: the round's execution row is the ground truth."""
        row, _ = self.store.enqueue_command('review_round', 88, now=1000.0)
        self.store.mark_dispatched(row['id'], 'm1', now=1001.0)
        self.store.ack_command(row['id'], 'm1', 'SPAWNED', now=1002.0,
                               execution='exec-1')
        self.store.db.execute(
            "INSERT INTO work_items(id,key,kind,version,payload,resources,state,"
            "priority,created_at) VALUES ('item-1','review:88','reviewer','v1','{}',"
            "'[]','READY',0,1000.0)")
        self.store.db.execute(
            "INSERT INTO executions(id,item_id,worker,token,expires_at,state,"
            "item_version,created_at) VALUES ('exec-1','item-1','m1',7,99999.0,"
            "'RUNNING','v1',1000.0)")
        self.store.db.commit()
        # still running -> the command stays live
        self.assertEqual(0, self.store.sweep_commands(now=1100.0)['finished'])
        self.assertEqual('SPAWNED', self.store.latest_command('review_round', 88)['state'])
        # execution terminal -> the command is done, and a re-click is a fresh one
        self.store.db.execute("UPDATE executions SET state='SUCCEEDED' WHERE id='exec-1'")
        self.store.db.commit()
        self.assertEqual(1, self.store.sweep_commands(now=1200.0)['finished'])
        self.assertEqual('DONE', self.store.latest_command('review_round', 88)['state'])
        fresh, created = self.store.enqueue_command('review_round', 88, now=1300.0)
        self.assertTrue(created)
        self.assertNotEqual(row['id'], fresh['id'])

    def test_spawned_command_can_be_acked_failed_by_its_holder(self):
        """issue #229: a session that dies silently after the grace window is
        failed by the liveness watcher — the SPAWNED command must accept a
        FAILED ack from its original holder (not just CLAIMED ones), so the
        item returns instead of waiting out the lease."""
        row, _ = self.store.enqueue_command('review_round', 88, now=1000.0)
        self.store.mark_dispatched(row['id'], 'm1', now=1001.0)
        self.store.ack_command(row['id'], 'm1', 'SPAWNED', now=1002.0,
                               execution='exec-1')
        self.assertEqual('SPAWNED', self.store.latest_command('review_round', 88)['state'])
        acked = self.store.ack_command(
            row['id'], 'm1', 'FAILED', fail_reason='PROCESS_DIED: session process exited',
            now=1003.0)
        self.assertIsNotNone(acked)
        self.assertEqual('FAILED', acked['state'])
        self.assertIn('PROCESS_DIED', acked['fail_reason'])
        # a different machine cannot fail someone else's spawned command
        self.assertIsNone(self.store.ack_command(
            row['id'], 'm2', 'FAILED', fail_reason='nope', now=1004.0))

    def test_failed_ack_is_a_noop_when_the_round_already_finished(self):
        """issue #235: the liveness watcher's FAILED ack must not overwrite a
        round that already succeeded — publish can land inside the 120s
        confirm window with the command still SPAWNED (sweep has no periodic
        timer), and the execution row, not the ack, is the ground truth."""
        row, _ = self.store.enqueue_command('review_round', 88, now=1000.0)
        self.store.mark_dispatched(row['id'], 'm1', now=1001.0)
        self.store.ack_command(row['id'], 'm1', 'SPAWNED', now=1002.0,
                               execution='exec-1')
        self.store.db.execute(
            "INSERT INTO work_items(id,key,kind,version,payload,resources,state,"
            "priority,created_at) VALUES ('item-1','review:88','reviewer','v1','{}',"
            "'[]','READY',0,1000.0)")
        self.store.db.execute(
            "INSERT INTO executions(id,item_id,worker,token,expires_at,state,"
            "item_version,created_at) VALUES ('exec-1','item-1','m1',7,99999.0,"
            "'SUCCEEDED','v1',1000.0)")
        self.store.db.commit()
        # round finished -> the FAILED ack is a CAS loss, command stays SPAWNED
        self.assertIsNone(self.store.ack_command(
            row['id'], 'm1', 'FAILED', fail_reason='PROCESS_DIED: session process exited',
            now=1003.0))
        self.assertEqual('SPAWNED', self.store.latest_command('review_round', 88)['state'])
        # a still-running execution still accepts the FAILED ack (issue #229)
        self.store.db.execute("UPDATE executions SET state='RUNNING' WHERE id='exec-1'")
        self.store.db.commit()
        acked = self.store.ack_command(
            row['id'], 'm1', 'FAILED', fail_reason='PROCESS_DIED: session process exited',
            now=1004.0)
        self.assertIsNotNone(acked)
        self.assertEqual('FAILED', acked['state'])

    def test_spawned_without_execution_closes_by_time_box(self):
        row, _ = self.store.enqueue_command('review_round', 88, now=1000.0)
        self.store.mark_dispatched(row['id'], 'm1', now=1001.0)
        self.store.ack_command(row['id'], 'm1', 'SPAWNED', now=1002.0)
        self.assertEqual(0, self.store.sweep_commands(now=1002.0 + 100)['finished'])
        self.assertEqual(1, self.store.sweep_commands(
            now=1002.0 + 14400, spawned_window=14400)['finished'])

    def test_sweep_requeues_unacked_then_fails_at_the_ratchet(self):
        row, _ = self.store.enqueue_command('review_round', 88, now=1000.0)
        self.store.mark_dispatched(row['id'], 'm1', now=1001.0)
        counts = self.store.sweep_commands(now=1001.0 + 600, ttl=1800, ack_window=600)
        self.assertEqual(1, counts['requeued'])
        row = self.store.latest_command('review_round', 88)
        self.assertEqual('PENDING', row['state'])
        self.assertEqual(1, row['attempts'])
        for attempt in (2, 3):
            self.store.mark_dispatched(row['id'], 'm1', now=1000.0 + attempt * 700)
            counts = self.store.sweep_commands(now=1000.0 + attempt * 700 + 600,
                                               ttl=1800, ack_window=600)
        self.assertEqual(1, counts['failed'])
        final = self.store.latest_command('review_round', 88)
        self.assertEqual(('FAILED', 'COMMAND_UNACKED'),
                         (final['state'], final['fail_reason']))


class ProgressStoreTests(unittest.TestCase):
    """progress_events: the agent-reported session stream behind a spawned
    review round. Storage-level contract: idempotent upsert by
    (execution, seq), ordered incremental reads, and retention swept with
    the command ledger's own repair pass."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self.tmp.name)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_append_progress_is_idempotent_by_execution_and_seq(self):
        added = self.store.append_progress('e1', [
            {'seq': 1, 'kind': 'assistant', 'text': '开始'},
            {'seq': 2, 'kind': 'tool', 'name': 'Bash'},
        ], now=1000.0)
        self.assertEqual(2, added)
        # re-reporting the same seqs (agent retry, server retry) adds nothing
        added = self.store.append_progress('e1', [
            {'seq': 1, 'kind': 'assistant', 'text': '开始'},
            {'seq': 3, 'kind': 'assistant', 'text': '继续'},
        ], now=1001.0)
        self.assertEqual(1, added)
        rows = self.store.progress_rows('e1')
        self.assertEqual([1, 2, 3], [r['seq'] for r in rows])
        self.assertEqual('继续', rows[2]['payload']['text'])

    def test_append_progress_validates_shape(self):
        with self.assertRaises(ValueError):
            self.store.append_progress('', [{'seq': 0}])
        with self.assertRaises(ValueError):
            self.store.append_progress('e1', [{'seq': 0}])
        with self.assertRaises(ValueError):
            self.store.append_progress('e1', [{'seq': -1}])
        with self.assertRaises(ValueError):
            self.store.append_progress('e1', [{'seq': True}])
        with self.assertRaises(ValueError):
            self.store.append_progress('e1', [{'seq': '0'}])
        self.assertEqual([], self.store.progress_rows('e1'))

    def test_progress_rows_reads_after_seq_in_order(self):
        self.store.append_progress('e1', [{'seq': i, 'kind': 'x'} for i in range(1, 6)])
        rows = self.store.progress_rows('e1', after_seq=3)
        self.assertEqual([4, 5], [r['seq'] for r in rows])
        # a different execution is fully isolated
        self.store.append_progress('e2', [{'seq': 1, 'kind': 'y'}])
        self.assertEqual([4, 5], [r['seq'] for r in self.store.progress_rows('e1', after_seq=3)])

    def _spawned_command(self, execution, *, created=1000.0, spawned=2000.0):
        row, _ = self.store.enqueue_command('review_round', 7, now=created)
        self.store.mark_dispatched(row['id'], 'm1', now=created + 1)
        self.store.ack_command(row['id'], 'm1', 'SPAWNED', execution=execution,
                               now=spawned)
        return row['id']

    def test_sweep_clears_progress_of_long_finished_rounds(self):
        self._spawned_command('e-done')          # closes to DONE on first sweep
        self.store.append_progress('e-done', [{'seq': 1, 'kind': 'x'}], now=2000.0)
        self.store.sweep_commands(now=2001.0)    # SPAWNED -> DONE (no such execution)
        # inside the retention window the stream survives (run-detail replay)
        self.store.sweep_commands(now=2001.0 + 6 * 86400)
        self.assertEqual(1, len(self.store.progress_rows('e-done')))
        # past the retention window the stream goes with the ledger row
        counts = self.store.sweep_commands(now=2001.0 + 8 * 86400)
        self.assertEqual([], self.store.progress_rows('e-done'))
        self.assertEqual(1, counts.get('progress_cleared', 0))

    def test_sweep_clears_orphan_streams_known_only_to_executions(self):
        """An ack-lost round (command reverted to PENDING, execution=NULL)
        still leaves the execution row: when that execution turns terminal,
        its reported stream must not leak forever (review MINOR)."""
        self.store.append_progress('e-orphan', [{'seq': 1, 'kind': 'x'}],
                                   now=2000.0)
        item = self.store.put_item('review:99', 'reviewer', 'v1', {}, ['path:*'])
        self.store.db.execute(
            "INSERT INTO executions (id, item_id, worker, token, expires_at,"
            " state, item_version, created_at) VALUES ('e-orphan', ?,"
            " 'w', 1, ?, 'FINISHED', 'v1', ?)",
            (item['id'], 2000.0, 1000.0))
        self.store.db.commit()
        counts = self.store.sweep_commands(now=2000.0 + 8 * 86400)
        self.assertEqual([], self.store.progress_rows('e-orphan'))
        self.assertEqual(1, counts['progress_cleared'])

    def test_sweep_keeps_progress_of_live_and_recent_commands(self):
        self._spawned_command('e-live')
        self.store.append_progress('e-live', [{'seq': 1, 'kind': 'x'}], now=2000.0)
        # a FAILED command inside the retention window keeps its stream too
        row, _ = self.store.enqueue_command('review_round', 8, now=9000.0)
        self.store.mark_dispatched(row['id'], 'm1', now=9000.5)
        self.store.ack_command(row['id'], 'm1', 'FAILED',
                               fail_reason='NOT_REGISTERED: x', now=9001.0)
        self.store.append_progress('e-live', [{'seq': 2, 'kind': 'x'}], now=9002.0)
        self.store.sweep_commands(now=9003.0)
        self.assertEqual([1, 2], [r['seq'] for r in self.store.progress_rows('e-live')])


class CommandServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service({'state_dir': str(Path(self.tmp.name) / 'state'),
                                'platform': {'url': 'http://g', 'repo': 'a/b'},
                                'project_key': 'a__b',
                                'snapshot_ttl': 3600})

    def tearDown(self):
        self.service.store.db.close()
        self.tmp.cleanup()

    def test_enqueue_validates_action_and_number(self):
        for action, number in (('explode', 88), ('review_round', 0),
                               ('review_round', -1), ('review_round', '88'),
                               ('review_round', True)):
            with self.assertRaises(ValueError) as caught:
                self.service.enqueue_command(action, number)
            self.assertIn('INVALID_COMMAND', str(caught.exception))

    def test_enqueue_validates_spawn_options(self):
        """The dialog's agent/model pair is a whitelist enum plus a strict
        model-id charset — the command payload stays doorbell-pure: enough to
        pick an argv preset in code, never enough to carry content."""
        bad = [
            {'agent': 'codex'},                                   # unknown agent
            {'agent': 7}, {'agent': ''},                          # not a non-empty str
            {'model': 'x/y'},                                     # model without agent
            {'agent': 'claude', 'model': 'x/y'},                  # claude has no picker
            {'agent': 'opencode', 'model': 'a/b; rm -rf'},        # injection chars
            {'agent': 'opencode', 'model': 'A B'},
            {'agent': 'opencode', 'model': '../etc/passwd'},
            {'agent': 'opencode', 'model': ''},                   # empty → pass None
            {'agent': 'opencode', 'model': 7},
        ]
        for kwargs in bad:
            with self.assertRaises(ValueError) as caught:
                self.service.enqueue_command('review_round', 88, **kwargs)
            self.assertIn('INVALID_COMMAND', str(caught.exception))
        # the happy path persists both values
        row, _ = self.service.enqueue_command(
            'review_round', 88, agent='opencode', model='kimi-for-coding/k3')
        self.assertEqual(('opencode', 'kimi-for-coding/k3'),
                         (row['agent'], row['model']))
        # claude is allowed but carries no model list — model stays None
        row, _ = self.service.enqueue_command('review_round', 89, agent='claude')
        self.assertEqual('claude', row['agent'])
        self.assertIsNone(row['model'])

    def test_ack_wrong_state_is_a_domain_rejection(self):
        self.service.enqueue_command('review_round', 88)
        with self.assertRaises(ValueError) as caught:
            self.service.ack_command(1, 'm1', 'SPAWNED')
        self.assertIn('COMMAND_STATE', str(caught.exception))

    def test_sweep_uses_configured_windows(self):
        self.service.cfg['command_ttl'] = 10
        self.service.enqueue_command('review_round', 88, now=100.0)
        counts = self.service.sweep_commands(now=111.0)
        self.assertEqual(1, counts['expired'])


class FakeConnection:
    """A scripted WS agent: send_json records, send failure raises."""

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail
        self.closed = False

    def send_json(self, payload):
        if self.fail:
            raise OSError('socket gone')
        self.sent.append(payload)

    def close(self):
        self.closed = True


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._home = os.environ.get('CODE_LOOP_HOME')
        os.environ['CODE_LOOP_HOME'] = self.tmp.name
        self.root = Path(self.tmp.name)
        cfg_dir = self.root / 'projects' / 'a__b'
        cfg_dir.mkdir(parents=True)
        (cfg_dir / 'control-plane.json').write_text(json.dumps(
            {'platform': {'url': 'http://g', 'repo': 'a/b'}, 'remote': True,
             'state_dir': str(cfg_dir / 'state'),
             'project_key': 'a__b', 'snapshot_ttl': 3600}), encoding='utf-8')
        self.coordinator = Coordinator(self.root / 'projects',
                                       self.root / 'registry.sqlite')
        self.registry = self.coordinator.registry()
        code = self.registry.create_enroll_code('agent-box', ['a__b'])
        out = self.registry.enroll(code, '1', '127.0.0.1')
        self.machine = self.registry.authenticate(out['token'])
        self.service = self.coordinator.service_for('a__b')

    def tearDown(self):
        self.registry.close()
        self.service.store.db.close()
        import gc
        gc.collect()
        self.tmp.cleanup()
        if self._home is None:
            os.environ.pop('CODE_LOOP_HOME', None)
        else:
            os.environ['CODE_LOOP_HOME'] = self._home

    def connect(self, fail=False, registrations=('a__b',),
                task_schema_versions=(1,)):
        conn = FakeConnection(fail=fail)
        self.coordinator.dispatcher.register(self.machine, conn)
        if registrations is not None:
            self.coordinator.dispatcher.hello(
                self.machine, list(registrations),
                task_schema_versions=list(task_schema_versions))
        return conn

    def test_enqueue_worker_task_keeps_offline_work_pending_then_dispatches(self):
        result = self.coordinator.dispatcher.enqueue_worker_task(
            'pr.repair', project='a__b', payload={'number': 88},
            source_event_id='pr-repair:source-1', dedupe_key='pr:a__b:88',
            now=1000.0)
        self.assertTrue(result['created'])
        self.assertEqual(0, result['dispatched'])
        self.assertEqual('PENDING', result['task']['state'])

        conn = self.connect()
        catchup = self.coordinator.dispatcher.dispatch_worker_tasks(
            machine_id=self.machine['id'], now=1001.0)
        self.assertEqual(1, catchup)
        self.assertEqual('worker_task', conn.sent[0]['command']['action'])
        self.assertEqual('pr.repair', conn.sent[0]['command']['task']['task_type'])

        replay = self.coordinator.dispatcher.enqueue_worker_task(
            'pr.repair', project='a__b', payload={'number': 88},
            source_event_id='pr-repair:source-1', dedupe_key='pr:a__b:88',
            now=1002.0)
        self.assertFalse(replay['created'])
        self.assertEqual(result['task']['id'], replay['task']['id'])

    def test_dispatch_pushes_pending_to_the_connected_machine(self):
        self.service.enqueue_command('review_round', 88, now=1000.0)
        conn = self.connect()
        dispatched = self.coordinator.dispatcher.dispatch(now=1000.0)
        self.assertEqual(1, dispatched)
        self.assertEqual({'type': 'command', 'command': {
            'id': self.service.latest_command('review_round', 88)['id'],
            'action': 'review_round', 'number': 88, 'project': 'a__b'}},
            conn.sent[0])
        row = self.service.latest_command('review_round', 88)
        self.assertEqual(('CLAIMED', self.machine['name']), (row['state'],
                                                             row['claimed_by']))

    def test_dispatch_frame_carries_spawn_options_when_set(self):
        """agent/model ride along only when present: default rounds keep the
        historical frame byte-identical (old agents never see new keys)."""
        self.service.enqueue_command('review_round', 88, now=1000.0,
                                     agent='opencode', model='kimi-for-coding/k3')
        conn = self.connect()
        self.assertEqual(1, self.coordinator.dispatcher.dispatch(now=1000.0))
        command = conn.sent[0]['command']
        self.assertEqual('opencode', command['agent'])
        self.assertEqual('kimi-for-coding/k3', command['model'])
        self.assertEqual(88, command['number'])

    def test_no_connected_agent_leaves_the_command_pending(self):
        self.service.enqueue_command('review_round', 88, now=1000.0)
        self.assertEqual(0, self.coordinator.dispatcher.dispatch(now=1000.0))
        self.assertEqual('PENDING', self.service.latest_command('review_round', 88)['state'])

    def test_unguarded_machine_is_skipped(self):
        other = self.root / 'projects' / 'c__d'
        other.mkdir(parents=True)
        (other / 'control-plane.json').write_text(json.dumps(
            {'platform': {'url': 'http://g', 'repo': 'c/d'}, 'remote': True,
             'state_dir': str(other / 'state'), 'project_key': 'c__d',
             'snapshot_ttl': 3600}), encoding='utf-8')
        self.connect()
        # 'agent-box' is granted for a__b only; c__d's command must not move.
        other_service = self.coordinator.service_for('c__d')
        try:
            other_service.enqueue_command('review_round', 7, now=1000.0)
            dispatched = self.coordinator.dispatcher.dispatch(now=1000.0)
            self.assertEqual(0, dispatched)
            self.assertEqual('PENDING',
                             other_service.latest_command('review_round', 7)['state'])
        finally:
            other_service.store.db.close()

    def test_send_failure_reverts_to_pending(self):
        self.service.enqueue_command('review_round', 88, now=1000.0)
        self.connect(fail=True)
        dispatched = self.coordinator.dispatcher.dispatch(now=1000.0)
        self.assertEqual(0, dispatched)
        row = self.service.latest_command('review_round', 88)
        self.assertEqual('PENDING', row['state'])
        self.assertIsNone(row['claimed_by'])

    def test_send_failure_falls_through_to_the_next_machine(self):
        """One dead socket must not park the command: the dispatcher drops
        exactly that connection and tries the next candidate."""
        code = self.registry.create_enroll_code('agent-2', ['a__b'])
        machine2 = self.registry.authenticate(
            self.registry.enroll(code, '1', '127.0.0.1')['token'])
        dead = self.connect(fail=True)
        healthy = FakeConnection()
        self.coordinator.dispatcher.register(machine2, healthy)
        # force the ordering: the dead machine is the least recently dispatched,
        # so the dispatcher must try (and drop) it before reaching the healthy one
        self.coordinator.dispatcher._last_dispatch[machine2['id']] = 1e9
        self.service.enqueue_command('review_round', 88, now=1000.0)
        dispatched = self.coordinator.dispatcher.dispatch(now=1000.0)
        self.assertEqual(1, dispatched)
        self.assertEqual(1, len(healthy.sent))
        with self.coordinator.dispatcher._lock:
            live = list(self.coordinator.dispatcher._connections.values())
        self.assertEqual([healthy], live)   # the dead one was dropped
        self.assertNotIn(self.machine['id'],
                         self.coordinator.dispatcher._connections)
        self.assertIsNot(dead, healthy)

    def test_send_failure_never_drops_a_newer_connection(self):
        """The exact race: the dispatcher snapshots a connection, and while the
        send fails the machine reconnects. Dropping "whatever is registered
        now" would kill the replacement — the connection object is the
        identity, so only the failed one is dropped."""
        def reconnecting_failure(payload):
            # the reconnect lands while this (already replaced) send fails
            self.coordinator.dispatcher.register(self.machine, fresh)
            raise OSError('socket gone')

        fresh = FakeConnection()
        dead = FakeConnection()
        dead.send_json = reconnecting_failure
        self.coordinator.dispatcher.register(self.machine, dead)
        self.service.enqueue_command('review_round', 88, now=1000.0)
        self.assertEqual(0, self.coordinator.dispatcher.dispatch(now=1000.0))
        with self.coordinator.dispatcher._lock:
            self.assertIs(fresh,
                          self.coordinator.dispatcher._connections[self.machine['id']])
        # …and the reverted command is delivered on the next dispatch
        self.assertEqual(1, self.coordinator.dispatcher.dispatch(now=1000.0))
        self.assertEqual(1, len(fresh.sent))

    def test_disabled_machine_stops_receiving(self):
        """Enablement is re-read from the registry at dispatch time, not from
        the snapshot taken when the socket was registered."""
        conn = self.connect()
        self.registry.set_enabled('agent-box', False)
        self.service.enqueue_command('review_round', 88, now=1000.0)
        self.assertEqual(0, self.coordinator.dispatcher.dispatch(now=1000.0))
        self.assertEqual([], conn.sent)
        self.assertEqual('PENDING',
                         self.service.latest_command('review_round', 88)['state'])

    def test_two_machines_single_winner(self):
        code = self.registry.create_enroll_code('agent-2', ['a__b'])
        out = self.registry.enroll(code, '1', '127.0.0.1')
        machine2 = self.registry.authenticate(out['token'])
        conn1 = self.connect()
        conn2 = FakeConnection()
        self.coordinator.dispatcher.register(machine2, conn2)
        self.service.enqueue_command('review_round', 88, now=1000.0)
        self.assertEqual(1, self.coordinator.dispatcher.dispatch(now=1000.0))
        sent = [c for c in (conn1, conn2) if c.sent]
        self.assertEqual(1, len(sent))
        row = self.service.latest_command('review_round', 88)
        self.assertEqual(1, row['attempts'])

    def test_reconnect_catchup_delivers_to_the_new_socket(self):
        self.service.enqueue_command('review_round', 88, now=1000.0)
        # machine_id-narrow dispatch is the connect-time catch-up path
        dispatched = self.coordinator.dispatcher.dispatch(
            machine_id=self.machine['id'], now=1000.0)
        self.assertEqual(0, dispatched)          # not connected yet
        conn = self.connect()
        dispatched = self.coordinator.dispatcher.dispatch(
            machine_id=self.machine['id'], now=1000.0)
        self.assertEqual(1, dispatched)
        self.assertEqual(1, len(conn.sent))

    def test_machine_task_dispatches_a_typed_frame_to_its_project_grant(self):
        task, created = self.registry.worker_tasks.enqueue(
            'worker.project_diagnose', project='a__b',
            payload={'loop': 'discover'},
            allowed_actions=['discover.run_once'],
            source_event_id='stall:a__b:discover:1', now=1000.0)
        self.assertTrue(created)
        conn = self.connect()
        self.assertEqual(1, self.coordinator.dispatcher.dispatch_worker_tasks(
            now=1000.0))
        sent = conn.sent[0]
        self.assertEqual('command', sent['type'])
        self.assertEqual('worker_task', sent['command']['action'])
        self.assertEqual({
            'id': task['id'], 'schema_version': 1,
            'task_type': 'worker.project_diagnose', 'project': 'a__b',
            'target_machine_id': self.machine['id'],
            'payload': {'loop': 'discover'},
            'allowed_actions': ['discover.run_once'],
            'expires_at': task['expires_at'],
            'source_event_id': 'stall:a__b:discover:1'},
            sent['command']['task'])
        current = self.registry.worker_tasks.get(task['id'])
        self.assertEqual(('CLAIMED', self.machine['id']),
                         (current['state'], current['claimed_by']))

    def test_legacy_worker_without_task_schema_keeps_task_pending(self):
        task, _ = self.registry.worker_tasks.enqueue(
            'pr.repair', project='a__b', payload={'number': 88},
            source_event_id='pr:legacy-worker')
        conn = FakeConnection()
        self.coordinator.dispatcher.register(self.machine, conn)
        self.coordinator.dispatcher.hello(self.machine, ['a__b'])

        self.assertEqual(0, self.coordinator.dispatcher.dispatch_worker_tasks())
        self.assertEqual([], conn.sent)
        self.assertEqual('PENDING', self.registry.worker_tasks.get(task['id'])['state'])

    def test_worker_receives_only_task_schemas_it_announced(self):
        task, _ = self.registry.worker_tasks.enqueue(
            'pr.repair', project='a__b', payload={'number': 88},
            source_event_id='pr:schema-v2')
        conn = self.connect(task_schema_versions=[2])

        self.assertEqual(0, self.coordinator.dispatcher.dispatch_worker_tasks())
        self.assertEqual([], conn.sent)
        self.assertEqual('PENDING', self.registry.worker_tasks.get(task['id'])['state'])

    def test_project_task_waits_for_local_registration_even_when_granted(self):
        task, _ = self.registry.worker_tasks.enqueue(
            'pr.repair', project='a__b', payload={'number': 88},
            source_event_id='pr:registration-required', now=1000.0)
        conn = self.connect(registrations=[])  # no local checkout is announced
        self.assertEqual(0, self.coordinator.dispatcher.dispatch_worker_tasks(
            now=1000.0))
        self.assertEqual([], conn.sent)
        self.assertEqual('PENDING', self.registry.worker_tasks.get(task['id'])['state'])

        self.coordinator.dispatcher.hello(self.machine, ['a__b'],
                                          task_schema_versions=[1])
        self.assertEqual(1, self.coordinator.dispatcher.dispatch_worker_tasks(
            now=1001.0))
        self.assertEqual('CLAIMED', self.registry.worker_tasks.get(task['id'])['state'])

    def test_expired_offline_task_requeues_after_ws_hello(self):
        from codeloop.cp.server import WsConnection
        class RecordingWsConnection(WsConnection):
            def __init__(self, machine, dispatcher):
                super().__init__(None, machine, dispatcher)
                self.sent = []

            def send_json(self, payload):
                self.sent.append(payload)

        task, _ = self.registry.worker_tasks.enqueue(
            'pr.repair', project='a__b', payload={'number': 88},
            source_event_id='pr:offline-expired', ttl=10, now=1000.0)
        self.registry.worker_tasks.sweep(now=1011.0)
        self.assertEqual('EXPIRED', self.registry.worker_tasks.get(task['id'])['state'])

        conn = RecordingWsConnection(self.machine, self.coordinator.dispatcher)
        self.coordinator.dispatcher.register(self.machine, conn)
        conn._handle_text(json.dumps(
            {'type': 'hello', 'registrations': ['a__b'],
             'task_schema_versions': [1]}).encode('utf-8'))

        current = self.registry.worker_tasks.get(task['id'])
        self.assertEqual('CLAIMED', current['state'])
        self.assertEqual(self.machine['id'], current['claimed_by'])
        self.assertEqual(task['id'], conn.sent[0]['command']['task']['id'])

    def test_expired_offline_task_waits_for_compatible_worker(self):
        task, _ = self.registry.worker_tasks.enqueue(
            'pr.repair', project='a__b', payload={'number': 89},
            source_event_id='pr:legacy-expired', ttl=10, now=1000.0)
        self.registry.worker_tasks.sweep(now=1011.0)
        conn = self.connect(task_schema_versions=[])

        self.assertEqual(0, self.coordinator.dispatcher.dispatch_worker_tasks(
            machine_id=self.machine['id']))
        self.assertEqual([], conn.sent)
        self.assertEqual('EXPIRED',
                         self.registry.worker_tasks.get(task['id'])['state'])

    def test_project_task_waits_for_authorized_machine_and_reverts_on_send_failure(self):
        task, _ = self.registry.worker_tasks.enqueue(
            'pr.repair', project='a__b', payload={'number': 88},
            source_event_id='pr:a__b:88:head-1', now=1000.0)
        dead = self.connect(fail=True)
        self.assertEqual(0, self.coordinator.dispatcher.dispatch_worker_tasks(
            now=1000.0))
        current = self.registry.worker_tasks.get(task['id'])
        self.assertEqual('PENDING', current['state'])
        self.assertIsNone(current['claimed_by'])
        self.assertTrue(dead.closed)

    def test_machine_task_with_project_does_not_dispatch_without_grant(self):
        task, _ = self.registry.worker_tasks.enqueue(
            'worker.project_diagnose', target_machine_id=self.machine['id'],
            project='other__repo', payload={'loop': 'discover'},
            source_event_id='stall:other__repo:discover:1', now=1000.0)
        conn = self.connect()
        self.assertEqual(0, self.coordinator.dispatcher.dispatch_worker_tasks(
            now=1000.0))
        self.assertEqual([], conn.sent)
        self.assertEqual('PENDING', self.registry.worker_tasks.get(task['id'])['state'])


class RepairTickTests(unittest.TestCase):
    """The repair pass ages rows out AND re-offers what it just re-queued —
    without it a command re-queued from an acked-but-dead round waits for the
    next click instead of reaching the agent that is online right now."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._home = os.environ.get('CODE_LOOP_HOME')
        os.environ['CODE_LOOP_HOME'] = self.tmp.name
        self.root = Path(self.tmp.name)
        cfg_dir = self.root / 'projects' / 'a__b'
        cfg_dir.mkdir(parents=True)
        (cfg_dir / 'control-plane.json').write_text(json.dumps(
            {'platform': {'url': 'http://g', 'repo': 'a/b'}, 'remote': True,
             'state_dir': str(cfg_dir / 'state'), 'project_key': 'a__b',
             'snapshot_ttl': 3600, 'command_ack_window': 1}), encoding='utf-8')
        from codeloop.cp.server import Coordinator
        self.coordinator = Coordinator(self.root / 'projects',
                                       self.root / 'registry.sqlite')
        self.registry = self.coordinator.registry()
        code = self.registry.create_enroll_code('agent-box', ['a__b'])
        self.machine = self.registry.authenticate(
            self.registry.enroll(code, '1', '127.0.0.1')['token'])
        self.service = self.coordinator.service_for('a__b')
        self.conn = FakeConnection()
        self.coordinator.dispatcher.register(self.machine, self.conn)

    def tearDown(self):
        self.registry.close()
        self.service.store.db.close()
        import gc
        gc.collect()
        self.tmp.cleanup()
        if self._home is None:
            os.environ.pop('CODE_LOOP_HOME', None)
        else:
            os.environ['CODE_LOOP_HOME'] = self._home

    def test_requeued_command_is_pushed_again_by_the_repair_pass(self):
        import cp_server
        # real timestamps here: the repair pass sweeps against the real clock
        row, _ = self.service.enqueue_command('review_round', 88)
        self.service.store.mark_dispatched(row['id'], 'agent-box',
                                          now=time.time() - 5)
        requeued = cp_server.repair_tick(self.coordinator,
                                        {'projects_root': str(self.root / 'projects')})
        self.assertEqual(1, requeued)
        # the client never acked: the repair re-queued it and re-pushed it
        self.assertEqual('CLAIMED',
                         self.service.latest_command('review_round', 88)['state'])
        self.assertEqual(1, len(self.conn.sent))


class ProgressVerbTests(unittest.TestCase):
    """command_progress: the agent-reported session stream lands in the
    project store only when the execution is one this ledger knows (spawned
    round or claimed execution), and re-reports never duplicate rows."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = Service({'state_dir': str(Path(self.tmp.name) / 'state'),
                                'platform': {'url': 'http://g', 'repo': 'a/b'},
                                'project_key': 'a__b',
                                'snapshot_ttl': 3600})

    def tearDown(self):
        self.service.store.db.close()
        self.tmp.cleanup()

    def _spawned_execution(self, execution='e1'):
        row, _ = self.service.enqueue_command('review_round', 7)
        self.service.store.mark_dispatched(row['id'], 'm1')
        self.service.ack_command(row['id'], 'm1', 'SPAWNED', execution=execution)

    def test_progress_appends_for_a_spawned_execution(self):
        self._spawned_execution()
        added = self.service.command_progress('e1', [
            {'seq': 1, 'kind': 'assistant', 'text': 'hi'}])
        self.assertEqual(1, added)
        self.assertEqual(1, self.service.command_progress(
            'e1', [{'seq': 1, 'kind': 'assistant', 'text': 'hi'},
                   {'seq': 2, 'kind': 'tool'}]))

    def test_progress_rejects_unknown_execution(self):
        with self.assertRaises(ValueError) as caught:
            self.service.command_progress('nope', [{'seq': 1}])
        self.assertIn('PROGRESS_EXECUTION', str(caught.exception))

    def test_progress_done_is_only_a_marker(self):
        self._spawned_execution()
        self.service.command_progress('e1', [{'seq': 1, 'kind': 'round_end'}],
                                      done=True)
        rows = self.service.store.progress_rows('e1')
        self.assertEqual(['round_end'], [r['payload']['kind'] for r in rows])


class HelloPreflightTests(unittest.TestCase):
    """The WS hello: a connecting agent announces which projects this machine
    holds registrations for; the dispatcher keeps them for the connection's
    lifetime and the preflight query reads them back per project."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._home = os.environ.get('CODE_LOOP_HOME')
        os.environ['CODE_LOOP_HOME'] = self.tmp.name
        self.root = Path(self.tmp.name)
        cfg_dir = self.root / 'projects' / 'a__b'
        cfg_dir.mkdir(parents=True)
        (cfg_dir / 'control-plane.json').write_text(json.dumps(
            {'platform': {'url': 'http://g', 'repo': 'a/b'}, 'remote': True,
             'state_dir': str(cfg_dir / 'state'),
             'project_key': 'a__b', 'snapshot_ttl': 3600}), encoding='utf-8')
        self.coordinator = Coordinator(self.root / 'projects',
                                       self.root / 'registry.sqlite')
        self.registry = self.coordinator.registry()
        code = self.registry.create_enroll_code('agent-box', ['a__b'])
        out = self.registry.enroll(code, '1', '127.0.0.1')
        self.machine = self.registry.authenticate(out['token'])

    def tearDown(self):
        self.registry.close()
        import gc
        gc.collect()
        self.tmp.cleanup()
        if self._home is None:
            os.environ.pop('CODE_LOOP_HOME', None)
        else:
            os.environ['CODE_LOOP_HOME'] = self._home

    def test_hello_announces_registrations(self):
        d = self.coordinator.dispatcher
        d.register(self.machine, FakeConnection())
        d.hello(self.machine, ['a__b', 'x__y'])
        regs = d.registrations(['a__b', 'x__y', 'z__z'])
        self.assertEqual({'online': ['agent-box'], 'registered': ['agent-box']},
                         regs['a__b'])
        # an ungranted project has no eligible candidate at all — the machine
        # is not "online but unregistered" for it, dispatch would never pick it
        self.assertEqual({'online': [], 'registered': []}, regs['x__y'])
        self.assertEqual({'online': [], 'registered': []}, regs['z__z'])

    def test_granted_machine_without_hello_counts_online_only(self):
        d = self.coordinator.dispatcher
        d.register(self.machine, FakeConnection())   # online, no hello yet
        code = self.registry.create_enroll_code('late-box', ['a__b'])
        out = self.registry.enroll(code, '1', '127.0.0.1')
        machine2 = self.registry.authenticate(out['token'])
        d.register(machine2, FakeConnection())
        # machine2 never sent hello: online yes, registered unknown
        self.assertEqual({'online': ['agent-box', 'late-box'], 'registered': []},
                         d.registrations(['a__b'])['a__b'])

    def test_unregister_clears_registrations(self):
        d = self.coordinator.dispatcher
        conn = FakeConnection()
        d.register(self.machine, conn)
        d.hello(self.machine, ['a__b'])
        d.unregister(self.machine['id'], conn)
        self.assertEqual({'online': [], 'registered': []},
                         d.registrations(['a__b'])['a__b'])

    def test_ws_text_hello_frame_reaches_the_dispatcher(self):
        from codeloop.cp.server import WsConnection
        d = self.coordinator.dispatcher
        conn = WsConnection(None, self.machine, d)
        d.register(self.machine, conn)
        conn._handle_text(json.dumps(
            {'type': 'hello', 'registrations': ['a__b']}).encode('utf-8'))
        self.assertEqual(['agent-box'], d.registrations(['a__b'])['a__b']['registered'])
        # a stray frame must not kill anything
        conn._handle_text(json.dumps({'type': 'what'}).encode('utf-8'))
        conn._handle_text(b'not json at all')

if __name__ == '__main__':
    unittest.main(verbosity=2)
