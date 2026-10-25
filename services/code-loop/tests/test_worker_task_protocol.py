"""Worker task frames are typed, versioned and cannot carry shell commands."""
import sys
import unittest
from pathlib import Path

for _p in (str(Path(__file__).resolve().parents[1]),
           str(Path(__file__).resolve().parents[1] / 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from codeloop.worker_task_protocol import validate_worker_task


class WorkerTaskProtocolTests(unittest.TestCase):
    def test_valid_pr_repair_envelope(self):
        command = {
            'id': 'task-1', 'action': 'worker_task', 'project': 'org__repo',
            'target_machine_id': 'machine-1',
            'task': {'id': 'task-1', 'schema_version': 1,
                     'task_type': 'pr.repair', 'project': 'org__repo',
                     'target_machine_id': 'machine-1',
                     'payload': {'number': 42, 'reasons': ['merge_conflict']},
                     'allowed_actions': [], 'expires_at': 9999,
                     'source_event_id': 'pr-repair:42'}}
        result = validate_worker_task(command, machine_id='machine-1', now=1000)
        self.assertEqual('pr.repair', result['task_type'])

    def test_rejects_unknown_task_extra_command_and_wrong_machine(self):
        base = {'id': 'task-1', 'action': 'worker_task', 'project': 'org__repo',
                'target_machine_id': 'machine-1',
                'task': {'id': 'task-1', 'schema_version': 1,
                         'task_type': 'worker.project_diagnose',
                         'project': 'org__repo', 'target_machine_id': 'machine-1',
                         'payload': {'loop': 'discover'}, 'allowed_actions': [],
                         'expires_at': 9999, 'source_event_id': 'stall:1'}}
        with self.assertRaisesRegex(ValueError, 'TASK_TYPE_UNKNOWN'):
            validate_worker_task(dict(base, task=dict(base['task'],
                                                       task_type='arbitrary.shell')),
                                 machine_id='machine-1', now=1000)
        with self.assertRaisesRegex(ValueError, 'TASK_MACHINE_MISMATCH'):
            validate_worker_task(base, machine_id='machine-2', now=1000)
        payload = dict(base['task']['payload'], command=['sh', '-c', 'whoami'])
        with self.assertRaisesRegex(ValueError, 'TASK_PAYLOAD_INVALID'):
            validate_worker_task(dict(base, task=dict(base['task'], payload=payload)),
                                 machine_id='machine-1', now=1000)

    def test_rejects_expired_task_or_unregistered_maintenance_action(self):
        base = {'id': 'task-1', 'action': 'worker_task', 'project': 'org__repo',
                'target_machine_id': 'machine-1',
                'task': {'id': 'task-1', 'schema_version': 1,
                         'task_type': 'worker.project_diagnose',
                         'project': 'org__repo', 'target_machine_id': 'machine-1',
                         'payload': {'loop': 'discover'},
                         'allowed_actions': ['discover.run_once'],
                         'expires_at': 999, 'source_event_id': 'stall:1'}}
        with self.assertRaisesRegex(ValueError, 'TASK_EXPIRED'):
            validate_worker_task(base, machine_id='machine-1', now=1000)
        base['task']['expires_at'] = 2000
        with self.assertRaisesRegex(ValueError, 'TASK_ACTION_UNSUPPORTED'):
            validate_worker_task(base, machine_id='machine-1', now=1000,
                                 local_actions=[])

    def test_pr_repair_cannot_receive_maintenance_permissions(self):
        command = {
            'id': 'task-1', 'action': 'worker_task', 'project': 'org__repo',
            'target_machine_id': 'machine-1',
            'task': {'id': 'task-1', 'schema_version': 1,
                     'task_type': 'pr.repair', 'project': 'org__repo',
                     'target_machine_id': 'machine-1',
                     'payload': {'number': 42, 'reasons': ['merge_conflict']},
                     'allowed_actions': ['scheduler.restart_registered'],
                     'expires_at': 9999, 'source_event_id': 'pr-repair:42'}}
        with self.assertRaisesRegex(ValueError, 'TASK_ACTIONS_INVALID'):
            validate_worker_task(command, machine_id='machine-1', now=1000)


if __name__ == '__main__':
    unittest.main()
