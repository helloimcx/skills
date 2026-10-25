"""Worker-local command journal prevents duplicate runtime invocations."""
import sys
import tempfile
import unittest
from pathlib import Path

for _p in (str(Path(__file__).resolve().parents[1]),
           str(Path(__file__).resolve().parents[1] / 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from codeloop.worker_task_store import WorkerTaskRunStore


class WorkerTaskRunStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = WorkerTaskRunStore(Path(self.tmp.name) / 'worker-tasks.sqlite')

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_begin_is_idempotent_and_terminal_result_is_durable(self):
        first, created = self.store.begin('command-1', 'pr.repair', 'org__repo', now=10)
        self.assertTrue(created)
        again, created = self.store.begin('command-1', 'pr.repair', 'org__repo', now=11)
        self.assertFalse(created)
        self.assertEqual(first['command_id'], again['command_id'])
        self.store.update('command-1', 'RUNNING', session_id='session-1', now=12)
        done = self.store.update('command-1', 'SUCCEEDED',
                                 result={'summary': 'done'}, now=13)
        self.assertEqual('SUCCEEDED', done['state'])
        self.assertEqual({'summary': 'done'}, done['result'])
        replay, created = self.store.begin('command-1', 'pr.repair', 'org__repo', now=14)
        self.assertFalse(created)
        self.assertEqual('SUCCEEDED', replay['state'])

    def test_failure_is_terminal_and_cannot_be_overwritten(self):
        self.store.begin('command-2', 'worker.project_diagnose', 'org__repo', now=10)
        failed = self.store.update('command-2', 'FAILED', fail_reason='runtime missing', now=11)
        self.assertEqual('FAILED', failed['state'])
        self.assertIsNone(self.store.update('command-2', 'SUCCEEDED', result={}, now=12))

    def test_failed_claim_fence_is_persisted_for_duplicate_terminal_report(self):
        self.store.begin('command-3', 'pr.repair', 'org__repo', now=10)
        failed = self.store.update('command-3', 'FAILED', execution_id='exec-3',
                                   claim_token=17, fail_reason='runtime missing', now=11)
        replay, created = self.store.begin('command-3', 'pr.repair', 'org__repo', now=12)
        self.assertFalse(created)
        self.assertEqual(('exec-3', 17),
                         (replay['execution_id'], replay['claim_token']))

    def test_running_runtime_process_identity_is_durable_for_restart_recovery(self):
        self.store.begin('command-4', 'worker.project_diagnose', 'org__repo', now=10)
        running = self.store.update('command-4', 'RUNNING', runtime_pid=1234,
                                    log_path='/tmp/task.log', now=11)
        self.assertEqual((1234, '/tmp/task.log'),
                         (running['runtime_pid'], running['log_path']))


if __name__ == '__main__':
    unittest.main()
