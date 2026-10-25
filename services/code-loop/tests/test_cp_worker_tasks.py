"""Machine-scoped Operator task ledger: durable, scoped and replay-safe."""
import sys
import tempfile
import unittest
from pathlib import Path

for _p in (str(Path(__file__).resolve().parents[1]),
           str(Path(__file__).resolve().parents[1] / 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from codeloop.cp.registry import Registry


class WorkerTaskLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = Registry(str(Path(self.tmp.name) / 'registry.sqlite'))
        self.tasks = self.registry.worker_tasks

    def tearDown(self):
        self.registry.close()
        self.tmp.cleanup()

    def test_enqueue_is_idempotent_by_source_and_active_scope(self):
        first, created = self.tasks.enqueue(
            'worker.project_diagnose', project='org__repo',
            payload={'loop': 'discover'},
            allowed_actions=['discover.run_once'],
            source_event_id='stall:machine:org__repo:discover:1',
            dedupe_key='machine:org__repo:discover', now=1000)
        self.assertTrue(created)
        replay, created = self.tasks.enqueue(
            'worker.project_diagnose', project='org__repo',
            payload={'loop': 'discover'},
            allowed_actions=['discover.run_once'],
            source_event_id='stall:machine:org__repo:discover:1',
            dedupe_key='machine:org__repo:discover', now=1001)
        self.assertFalse(created)
        self.assertEqual(first['id'], replay['id'])

        duplicate, created = self.tasks.enqueue(
            'worker.project_diagnose', project='org__repo',
            payload={'loop': 'discover'},
            source_event_id='stall:machine:org__repo:discover:2',
            dedupe_key='machine:org__repo:discover', now=1002)
        self.assertFalse(created)
        self.assertEqual(first['id'], duplicate['id'])

    def test_machine_claim_and_result_are_bound_to_claimant(self):
        task, _ = self.tasks.enqueue(
            'pr.repair', project='org__repo', target_machine_id='machine-1',
            payload={'number': 42, 'repair_reasons': ['merge_conflict']},
            source_event_id='cp:org__repo:17', now=1000)
        claimed = self.tasks.mark_dispatched(task['id'], 'machine-1', now=1001)
        self.assertEqual(('CLAIMED', 'machine-1'),
                         (claimed['state'], claimed['claimed_by']))
        self.assertIsNone(self.tasks.mark_dispatched(task['id'], 'machine-2', now=1002))
        self.assertIsNone(self.tasks.report(
            task['id'], 'machine-2', 'RUNNING', session_id='session-1', now=1003))

        running = self.tasks.report(
            task['id'], 'machine-1', 'RUNNING', session_id='session-1', now=1003)
        self.assertEqual(('RUNNING', 'session-1'),
                         (running['state'], running['session_id']))
        done = self.tasks.report(
            task['id'], 'machine-1', 'SUCCEEDED',
            result={'summary': 'fixed', 'actions': []}, now=1004)
        self.assertEqual('SUCCEEDED', done['state'])
        self.assertEqual({'summary': 'fixed', 'actions': []}, done['result'])
        self.assertIsNone(self.tasks.report(
            task['id'], 'machine-1', 'FAILED', fail_reason='late', now=1005))

    def test_unknown_task_and_non_object_payload_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'TASK_TYPE_UNKNOWN'):
            self.tasks.enqueue('arbitrary.shell', payload={'command': 'echo hi'})
        with self.assertRaisesRegex(ValueError, 'TASK_PAYLOAD_INVALID'):
            self.tasks.enqueue('worker.project_diagnose', payload=['not', 'an', 'object'])

    def test_state_transitions_emit_machine_scoped_replayable_events(self):
        task, _ = self.tasks.enqueue(
            'worker.project_diagnose', project='org__repo',
            target_machine_id='machine-1', payload={'loop': 'discover'},
            source_event_id='stall:machine-1:1', now=1000)
        self.tasks.mark_dispatched(task['id'], 'machine-1', now=1001)
        self.tasks.report(task['id'], 'machine-1', 'RUNNING', now=1002)
        self.tasks.report(task['id'], 'machine-1', 'RUNNING', now=1003)
        self.tasks.report(task['id'], 'machine-1', 'SUCCEEDED',
                          result={'summary': 'done'}, now=1004)

        rows = self.tasks.events_after('machine-1', 0)
        self.assertEqual(['worker.task.running', 'worker.task.succeeded'],
                         [row['kind'] for row in rows])
        self.assertEqual('org__repo', rows[-1]['project'])
        self.assertEqual(rows[-1]['id'], rows[-1]['payload']['event_id'])
        self.assertEqual([], self.tasks.events_after('machine-2', 0))

    def test_stale_running_task_becomes_failed_with_replayable_event(self):
        task, _ = self.tasks.enqueue(
            'worker.project_diagnose', project='org__repo',
            target_machine_id='machine-1', payload={'loop': 'discover'},
            source_event_id='stall:machine-1:2', now=1000)
        self.tasks.mark_dispatched(task['id'], 'machine-1', now=1001)
        self.tasks.report(task['id'], 'machine-1', 'RUNNING', now=1002)
        counts = self.tasks.sweep(now=1010, running_timeout=8)
        self.assertEqual(1, counts['failed'])
        self.assertEqual('FAILED', self.tasks.get(task['id'])['state'])
        events = self.tasks.events_after('machine-1', 0)
        self.assertEqual('worker.task.failed', events[-1]['kind'])
        self.assertEqual('TASK_RUNNING_TIMEOUT',
                         events[-1]['payload']['fail_reason'])

    def test_expired_offline_task_requeues_only_on_registered_granted_machine(self):
        task, _ = self.tasks.enqueue(
            'pr.repair', project='org__repo', payload={'number': 42},
            source_event_id='pr:offline:42', ttl=10, now=1000)
        self.tasks.sweep(now=1010)
        self.assertEqual(0, self.tasks.requeue_expired_for_machine(
            'machine-1', [], now=1011, ttl=100))
        self.assertEqual(1, self.tasks.requeue_expired_for_machine(
            'machine-1', ['org__repo'], now=1011, ttl=100))
        pending = self.tasks.get(task['id'])
        self.assertEqual('PENDING', pending['state'])
        self.assertEqual(1111, pending['expires_at'])

    def test_expired_machine_targeted_project_task_needs_grant(self):
        task, _ = self.tasks.enqueue(
            'worker.project_diagnose', project='org__repo',
            target_machine_id='machine-1', payload={'loop': 'discover'},
            source_event_id='stall:machine-1:offline', ttl=10, now=1000)
        self.tasks.sweep(now=1010)
        self.assertEqual(0, self.tasks.requeue_expired_for_machine(
            'machine-1', [], now=1011))
        self.assertEqual(0, self.tasks.requeue_expired_for_machine(
            'machine-1', ['other__repo'], now=1011))
        self.assertEqual('EXPIRED', self.tasks.get(task['id'])['state'])
        self.assertEqual(1, self.tasks.requeue_expired_for_machine(
            'machine-1', ['org__repo'], now=1011))
        self.assertEqual('PENDING', self.tasks.get(task['id'])['state'])

    def test_expired_pending_task_is_not_dispatched(self):
        task, _ = self.tasks.enqueue(
            'worker.project_diagnose', project='org__repo',
            payload={'loop': 'discover'}, ttl=30, now=1000)
        self.assertEqual(1, self.tasks.sweep(now=1030)['expired'])
        self.assertEqual('EXPIRED', self.tasks.get(task['id'])['state'])
        self.assertIsNone(self.tasks.mark_dispatched(task['id'], 'machine-1', now=1031))

    def test_unacked_claim_requeues_then_fails_at_attempt_limit(self):
        task, _ = self.tasks.enqueue(
            'worker.project_diagnose', project='org__repo',
            payload={'loop': 'discover'}, now=1000)
        self.tasks.mark_dispatched(task['id'], 'machine-1', now=1001)
        self.assertEqual({'expired': 0, 'requeued': 1, 'failed': 0},
                         self.tasks.sweep(now=1011, ack_window=10, max_attempts=2))
        current = self.tasks.get(task['id'])
        self.assertEqual(('PENDING', None, 1),
                         (current['state'], current['claimed_by'], current['attempts']))
        self.tasks.mark_dispatched(task['id'], 'machine-1', now=1012)
        self.assertEqual({'expired': 0, 'requeued': 0, 'failed': 1},
                         self.tasks.sweep(now=1022, ack_window=10, max_attempts=2))
        self.assertEqual('FAILED', self.tasks.get(task['id'])['state'])


if __name__ == '__main__':
    unittest.main()
