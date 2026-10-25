"""Worker task result writes are bound to the authenticated machine."""
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path

for _p in (str(Path(__file__).resolve().parents[1]),
           str(Path(__file__).resolve().parents[1] / 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from codeloop.cp import server
from codeloop.cp.protocol import ProtocolError
from codeloop.cp.registry import Registry
from codeloop.cp.worker import WorkerService


class WorkerTaskReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = Registry(str(Path(self.tmp.name) / 'registry.sqlite'))
        self.machine = {'id': 'machine-1', 'name': 'worker-1'}
        self.task, _ = self.registry.worker_tasks.enqueue(
            'worker.project_diagnose', target_machine_id=self.machine['id'],
            project='org__repo', payload={'loop': 'discover'},
            source_event_id='stall:org__repo:discover:1', now=1000)
        self.registry.worker_tasks.mark_dispatched(
            self.task['id'], self.machine['id'], now=1001)

    def tearDown(self):
        self.registry.close()
        self.tmp.cleanup()

    def test_report_records_session_and_terminal_result(self):
        running = server._invoke_worker_task_report(
            self.registry, {'task': self.task['id'], 'state': 'RUNNING',
                            'session_id': 'session-1'}, self.machine, now=1002)
        self.assertEqual(('RUNNING', 'session-1'),
                         (running['state'], running['session_id']))
        done = server._invoke_worker_task_report(
            self.registry, {'task': self.task['id'], 'state': 'SUCCEEDED',
                            'session_id': 'session-1',
                            'result': {'summary': 'discover stalled', 'actions': []}},
            self.machine, now=1003)
        self.assertEqual('SUCCEEDED', done['state'])
        self.assertEqual({'summary': 'discover stalled', 'actions': []}, done['result'])

    def test_other_machine_cannot_report_for_claimant(self):
        with self.assertRaises(ProtocolError) as caught:
            server._invoke_worker_task_report(
                self.registry, {'task': self.task['id'], 'state': 'SUCCEEDED',
                                'result': {'summary': 'spoof'}},
                {'id': 'machine-2', 'name': 'worker-2'}, now=1002)
        self.assertEqual('TASK_NOT_OWNED', caught.exception.code)
        self.assertEqual('CLAIMED', self.registry.worker_tasks.get(
            self.task['id'])['state'])

    def test_malformed_task_report_is_rejected(self):
        with self.assertRaises(ProtocolError) as caught:
            server._invoke_worker_task_report(
                self.registry, {'task': self.task['id'], 'state': 'SUCCEEDED',
                                'result': 'plain text'}, self.machine, now=1002)
        self.assertEqual('INVALID_REQUEST', caught.exception.code)

    def test_worker_can_replay_its_completion_events_and_results(self):
        running = server._invoke_worker_task_report(
            self.registry, {'task': self.task['id'], 'state': 'RUNNING'},
            self.machine, now=1002)
        done = server._invoke_worker_task_report(
            self.registry, {'task': self.task['id'], 'state': 'SUCCEEDED',
                            'result': {'summary': 'diagnosed'}},
            self.machine, now=1003)
        listed = server._invoke_worker_task_list(
            self.registry, {}, self.machine)
        events = server._invoke_worker_task_events(
            self.registry, {'after': '0', 'limit': '10'}, self.machine)

        self.assertEqual('SUCCEEDED', listed['tasks'][0]['state'])
        self.assertEqual({'summary': 'diagnosed'}, listed['tasks'][0]['result'])
        self.assertEqual(['worker.task.running', 'worker.task.succeeded'],
                         [row['kind'] for row in events['events']])
        self.assertEqual(events['events'][-1]['id'], events['next_cursor'])
        self.assertEqual('RUNNING', running['state'])
        self.assertEqual('SUCCEEDED', done['state'])

    def test_failed_pr_report_can_release_claim_using_its_token(self):
        repair, _ = self.registry.worker_tasks.enqueue(
            'pr.repair', target_machine_id=self.machine['id'], project='org__repo',
            payload={'number': 42}, source_event_id='pr:42', now=1000)
        self.registry.worker_tasks.mark_dispatched(repair['id'], self.machine['id'], now=1001)
        failed = self.registry.worker_tasks.report(
            repair['id'], self.machine['id'], 'FAILED', execution_id='exec-1',
            fail_reason='runtime failed', now=1002)

        class StoreHandle:
            def __init__(self):
                self.closed = False
                self.db = self

            def close(self):
                self.closed = True

        class ProjectService:
            def __init__(self):
                self.calls = []
                self.store = StoreHandle()

            def abandon(self, execution, token):
                self.calls.append((execution, token))

        class Coordinator:
            def __init__(self, service):
                self.service = service
                self.projects = []

            def service_for(self, project):
                self.projects.append(project)
                return self.service

        service = ProjectService()
        coordinator = Coordinator(service)
        server._release_worker_task_claim(coordinator, failed, 'exec-1', 9)
        self.assertEqual(['org__repo'], coordinator.projects)
        self.assertEqual([('exec-1', 9)], service.calls)
        self.assertTrue(service.store.closed)

    def test_fence_token_cannot_be_reported_without_execution_id(self):
        with self.assertRaises(ProtocolError) as caught:
            server._invoke_worker_task_report(
                self.registry, {'task': self.task['id'], 'state': 'FAILED',
                                'token': 5}, self.machine, now=1002)
        self.assertEqual('INVALID_REQUEST', caught.exception.code)

    def test_active_executions_is_scoped_to_the_requested_loop(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        db.executescript('''
            CREATE TABLE work_items(id TEXT, key TEXT, kind TEXT);
            CREATE TABLE executions(id TEXT, item_id TEXT, worker TEXT, state TEXT,
                                    created_at REAL, last_seen_at REAL, expires_at REAL);
            INSERT INTO work_items VALUES('d1','discover','discover');
            INSERT INTO work_items VALUES('h1','repair:42','handler');
            INSERT INTO executions VALUES('exec-discover','d1','worker/discover',
                                          'RUNNING',1,2,9999999999);
            INSERT INTO executions VALUES('exec-handler','h1','worker/handler',
                                          'RUNNING',1,2,9999999999);
        ''')
        service = type('Service', (), {'store': type('Store', (), {'db': db})()})()
        try:
            self.assertEqual(['exec-discover'],
                             [row['id'] for row in server._active_executions(
                                 service, 'discover')])
            self.assertEqual({'exec-discover', 'exec-handler'},
                             {row['id'] for row in server._active_executions(service)})
            self.assertEqual({'exec-discover', 'exec-handler'},
                             {row['id'] for row in server._recent_executions(
                                 service, limit=20)})
        finally:
            db.close()

    def test_exact_execution_status_is_queryable_for_handler_completion(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        db.executescript('''
            CREATE TABLE work_items(id TEXT, key TEXT, kind TEXT);
            CREATE TABLE executions(id TEXT, item_id TEXT, worker TEXT, state TEXT,
                                    created_at REAL, last_seen_at REAL, expires_at REAL);
            INSERT INTO work_items VALUES('h1','repair:42','handler');
            INSERT INTO executions VALUES('exec-handler','h1','worker/handler',
                                          'RUNNING',1,2,9999999999);
        ''')
        service = type('Service', (), {'store': type('Store', (), {'db': db})()})()
        try:
            self.assertEqual({'id': 'exec-handler', 'state': 'RUNNING',
                              'expires_at': 9999999999},
                             server._execution_status(service, 'exec-handler'))
            self.assertIsNone(server._execution_status(service, 'missing'))
        finally:
            db.close()

    def test_project_execution_query_requires_machine_grant(self):
        class RegistryStub:
            def require_write_access(self, machine, project):
                raise server.RegistryError('PROJECT_NOT_GRANTED')

            def close(self):
                pass

        class CoordinatorStub:
            def registry(self):
                return RegistryStub()

            def service_for(self, project):
                raise AssertionError('ungranted machine reached project service')

        handler = server.Handler.__new__(server.Handler)
        handler.coordinator = CoordinatorStub()
        with self.assertRaises(ProtocolError) as caught:
            handler._dispatch_project('other__project', 'executions', {}, self.machine)
        self.assertEqual('PROJECT_NOT_GRANTED', caught.exception.code)

    def test_worker_service_calls_execution_and_abandon_endpoints(self):
        class Client:
            def __init__(self):
                self.calls = []

            def endpoint(self, verb):
                return '/api/projects/org__repo/' + verb

            def call(self, method, path, body=None):
                self.calls.append((method, path, body))
                return {'executions': [{'id': 'exec-1'}],
                        'execution': {'id': 'exec-1', 'state': 'SUCCEEDED'}}

        client = Client()
        worker = WorkerService({'server': {'url': 'http://cp.example',
                                           'project': 'org__repo'},
                                'state_dir': str(Path(self.tmp.name) / 'state')},
                               client=client)
        self.assertEqual([{'id': 'exec-1'}], worker.executions(loop='discover'))
        self.assertEqual([{'id': 'exec-1'}],
                         worker.recent_executions(loop='discover', limit=5))
        self.assertEqual({'id': 'exec-1', 'state': 'SUCCEEDED'},
                         worker.execution_status('exec-1'))
        worker.abandon('exec-1', 9)
        self.assertEqual([
            ('GET', '/api/projects/org__repo/executions?loop=discover', None),
            ('GET', '/api/projects/org__repo/executions?include_terminal=1&limit=5&loop=discover', None),
            ('GET', '/api/projects/org__repo/executions?execution_id=exec-1', None),
            ('POST', '/api/projects/org__repo/abandon',
             {'execution': 'exec-1', 'token': 9})], client.calls)


if __name__ == '__main__':
    unittest.main()
