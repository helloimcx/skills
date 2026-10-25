#!/usr/bin/env python3
"""Typed worker tasks run through registered local runtimes and adapters."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codeloop import worker_task_executor as executor_mod


class _Runner:
    def __init__(self):
        self.calls = []
        self.state = 'ready'

    def query_scheduler(self, registration):
        self.calls.append(('query_scheduler', registration))
        return {'exists': True, 'enabled': True, 'state': self.state}

    def start_scheduler(self, registration):
        self.calls.append(('start_scheduler', registration))
        self.state = 4
        return {'started': True}

    def restart_scheduler(self, registration):
        self.calls.append(('restart_scheduler', registration))
        self.state = 4
        return {'restarted': True}

    def query_service(self, registration):
        self.calls.append(('query_service', registration))
        return {'exists': True, 'state': 'stopped'}

    def start_service(self, registration):
        self.calls.append(('start_service', registration))
        return {'started': True}


class _Agent:
    def __init__(self, root, *, task_type='worker.project_diagnose',
                 runtime_result=None, active_executions=None):
        self.root = root
        self.runtime_result = runtime_result or subprocess.CompletedProcess(
            ['runtime'], 0, stdout='{"summary":"worker stalled","findings":[],"actions":[]}',
            stderr='')
        self.runtime_calls = []
        self.runtime_argv = []
        self.db_path = str(root / 'dashboard.sqlite')
        self.worker = self
        self.git_fn = None
        self.active_executions = active_executions or []
        self.handler_execution_state = None
        self.abandoned = []
        self.row = {
            'org_repo': 'acme__app', 'repo_cwd': str(root / 'repo'),
            'entry_doc': str(root / 'entry.md'), 'rules_doc': str(root / 'rules.md'),
            'spawn_command': json.dumps(['claude', '-p']),
            'coordinator_agent': 'opencode', 'coordinator_model': None,
            'config_path': str(root / 'control-plane.json')}
        (root / 'repo').mkdir(exist_ok=True)
        (root / 'entry.md').write_text('entry', encoding='utf-8')
        (root / 'rules.md').write_text('rules', encoding='utf-8')
        self.cfg = {'repo_root': str(root / 'repo'), 'state_dir': str(root / 'state'),
                    'maintenance': {'scheduler_tasks': {'discover': {
                        'name': 'CodeLoop-Discover', 'path': '\\'}},
                        'services': {'worker_agent': 'CodeLoopWorkerAgent'}}}

    def _task_registration(self, project, require_handler=False):
        return self.row

    def cfg_factory(self, project, row):
        return self.cfg

    def worker_factory(self, cfg):
        return self

    def _coordinator_runtime(self, row, cfg, task_type):
        return ('opencode', None, ['opencode', 'run', '--auto'])

    def _load_cfg(self, project, row):
        return self.cfg

    def _run_worker_runtime(self, argv, prompt, cwd, log_path, env=None):
        self.runtime_calls.append((argv, prompt, cwd, log_path, env))
        return self.runtime_result

    def status(self):
        return {'executions': {'RUNNING': len(self.active_executions)}}

    def items(self, kind=None):
        return []

    def executions(self, loop=None):
        return list(self.active_executions)

    def recent_executions(self, loop=None, limit=20):
        return [{'id': 'exec-old', 'state': 'SUCCEEDED', 'loop': loop,
                 'created_at': 0}]

    def execution_status(self, execution):
        return {'id': execution, 'state': self.handler_execution_state}

    def abandon(self, execution, token):
        self.abandoned.append((execution, token))


class _Process:
    def __init__(self, pid, returncode):
        self.pid = pid
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def task(task_type='worker.project_diagnose', *, actions=None, payload=None):
    return {'id': 'task-1', 'schema_version': 1, 'task_type': task_type,
            'project': 'acme__app', 'target_machine_id': 'machine-1',
            'payload': payload or {'loop': 'discover', 'last_execution_id': 'exec-old',
                                   'age_seconds': 80000, 'threshold_seconds': 600},
            'allowed_actions': actions or [], 'expires_at': 9999999999,
            'source_event_id': 'stall:acme__app:discover'}


class WorkerTaskExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.agent = _Agent(self.root)
        def popen(argv, *, cwd, stdin, stdout, stderr, env, **kwargs):
            self.agent.runtime_argv = list(argv)
            prompt_text = stdin.read().decode('utf-8')
            self.agent.runtime_calls.append(
                (argv[:-1], prompt_text, cwd, stdout.name, env))
            stdout.write((self.agent.runtime_result.stdout or '') +
                         (self.agent.runtime_result.stderr or ''))
            return _Process(1234, self.agent.runtime_result.returncode)

        self.agent.agent_popen_fn = popen
        self.agent.agent_run_fn = lambda argv, **kwargs: self.agent.runtime_calls.append(
            (argv[:-1], argv[-1], kwargs['cwd'], str(self.root / 'runtime.log'),
             kwargs.get('env'))) or self.agent.runtime_result
        self.adapter = _Runner()
        self.executor = executor_mod.WorkerTaskExecutor(
            self.agent, maintenance_adapter=self.adapter,
            watchdog_log=self.root / 'watchdog.jsonl', clock=lambda: 1000)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pr_repair_starts_one_coordinator_and_delegates_handler_child(self):
        self.agent.handler_execution_state = 'SUCCEEDED'
        self.agent.runtime_result = subprocess.CompletedProcess(
            ['runtime'], 0,
            stdout='\n'.join((
                json.dumps({'type': 'tool_use', 'sessionID': 'ses_parent',
                            'part': {'id': 'call_handler', 'tool': 'task',
                                     'state': {'status': 'completed',
                                               'output': {'sessionId': 'ses_child'}}}}),
                json.dumps({'type': 'text', 'sessionID': 'ses_parent',
                            'part': {'type': 'text', 'text':
                                     '{"summary":"handled","handler_session_id":"ses_fake"}'}}))),
            stderr='')
        task_in = task('pr.repair', payload={'number': 42, 'reasons': [
            'changes_requested', 'merge_conflict'], 'head_sha': 'abc',
            'base_ref': 'main', 'base_sha': 'base-old'})
        prepared = {'execution': 'execution-1', 'token': 7,
                    'worktree': str(self.root / 'worktree'), 'branch': 'fix/repair-pr-42',
                    'head_ref': 'feature/example', 'task': 'PR_REVIEW_FIX',
                    'repair_reasons': ['changes_requested', 'merge_conflict'],
                    'head_sha': 'abc', 'base_ref': 'main', 'base_sha': 'base-new'}
        with patch.object(executor_mod.review_round, 'prepare_round',
                          return_value=prepared) as prepare, \
                patch.object(executor_mod.trigger, 'build_round_prompt',
                             return_value='prepared handler prompt'):
            result = self.executor(task_in)

        prepare.assert_called_once()
        self.assertEqual('execution-1', result['execution_id'])
        self.assertEqual('execution-1', result['handler']['execution_id'])
        self.assertEqual(('ses_child', 'call_handler'),
                         (result['handler']['session_id'],
                          result['handler']['tool_call_id']))
        self.assertIn('independent Handler subagent', self.agent.runtime_calls[0][1])
        self.assertIn('prepared handler prompt', self.agent.runtime_calls[0][1])
        self.assertEqual(executor_mod.RUNTIME_STDIN_MARKER,
                         self.agent.runtime_argv[-1])
        self.assertNotIn('token=7', ' '.join(self.agent.runtime_argv))
        self.assertFalse(Path(self.agent.runtime_calls[0][3]).with_suffix(
            '.prompt').exists())
        self.assertEqual(str(self.root / 'worktree'), self.agent.runtime_calls[0][2])

    def test_completed_task_tool_with_active_cp_execution_is_not_success(self):
        self.agent.handler_execution_state = 'RUNNING'
        self.agent.runtime_result = subprocess.CompletedProcess(
            ['runtime'], 0,
            stdout=json.dumps({'type': 'tool_use', 'sessionID': 'ses_parent',
                               'part': {'id': 'call_handler', 'tool': 'task',
                                        'state': {'status': 'completed'}}}),
            stderr='')
        task_in = task('pr.repair', payload={'number': 42, 'reasons': ['merge_conflict'],
            'head_sha': 'abc', 'base_ref': 'main'})
        prepared = {'execution': 'execution-1', 'token': 7,
                    'worktree': str(self.root / 'worktree'), 'branch': 'fix/repair-pr-42',
                    'head_ref': 'feature/example', 'task': 'PR_CONFLICT_RESOLUTION',
                    'repair_reasons': ['merge_conflict'], 'head_sha': 'abc',
                    'base_ref': 'main'}
        with patch.object(executor_mod.review_round, 'prepare_round',
                          return_value=prepared), \
                patch.object(executor_mod.trigger, 'build_round_prompt',
                             return_value='prepared handler prompt'):
            with self.assertRaisesRegex(executor_mod.WorkerTaskExecutionError,
                                        'HANDLER_EXECUTION_NOT_SUCCEEDED') as caught:
                self.executor(task_in)
        self.assertEqual(('execution-1', 7),
                         (caught.exception.execution_id, caught.exception.token))

    def test_coordinator_cannot_report_success_without_a_native_task_call(self):
        self.agent.runtime_result = subprocess.CompletedProcess(
            ['runtime'], 0,
            stdout='{"type":"text","sessionID":"ses_parent","part":{"type":"text","text":"{\\"summary\\":\\"pretend\\",\\"handler_session_id\\":\\"ses_fake\\"}"}}',
            stderr='')
        task_in = task('pr.repair', payload={'number': 42, 'reasons': ['merge_conflict']})
        prepared = {'execution': 'execution-1', 'token': 7,
                    'worktree': str(self.root / 'worktree'), 'branch': 'fix/repair-pr-42',
                    'head_ref': 'feature/example', 'task': 'PR_CONFLICT_RESOLUTION',
                    'repair_reasons': ['merge_conflict'], 'head_sha': 'abc'}
        with patch.object(executor_mod.review_round, 'prepare_round',
                          return_value=prepared), \
                patch.object(executor_mod.trigger, 'build_round_prompt',
                             return_value='prepared handler prompt'):
            with self.assertRaisesRegex(executor_mod.WorkerTaskExecutionError,
                                        'HANDLER_CHILD_UNCONFIRMED'):
                self.executor(task_in)

    def test_diagnosis_uses_evidence_and_only_executes_authorized_action_ids(self):
        self.agent.runtime_result = subprocess.CompletedProcess(
            ['runtime'], 0,
            stdout='{"summary":"scheduler stopped","findings":[],"actions":[{"id":"scheduler.start_registered"},{"id":"bash.rm"}]}',
            stderr='')
        result = self.executor(task(actions=['scheduler.start_registered']))

        self.assertIn('worker', result['evidence'])
        self.assertIn('watchdog', result['evidence'])
        self.assertEqual('exec-old', result['evidence']['control_plane'][
            'recent_discover_executions'][0]['id'])
        self.assertIn('last_execution_id', self.agent.runtime_calls[0][1])
        self.assertEqual(['query_scheduler', 'query_scheduler', 'start_scheduler',
                          'query_scheduler'],
                         [call[0] for call in self.adapter.calls])
        self.assertEqual('SUCCEEDED', result['actions'][0]['state'])
        self.assertIn('bash.rm', result['rejected_actions'])

    def test_discover_rerun_is_refused_while_discover_execution_is_active(self):
        self.agent.active_executions = [{'id': 'exec-active', 'loop': 'discover',
                                         'state': 'RUNNING'}]
        self.agent.runtime_result = subprocess.CompletedProcess(
            ['runtime'], 0,
            stdout='{"summary":"run it","findings":[],"actions":[{"id":"discover.run_once"}]}',
            stderr='')
        result = self.executor(task(actions=['discover.run_once']))

        self.assertEqual('REFUSED_ACTIVE_EXECUTION', result['actions'][0]['state'])
        self.assertNotIn('discover.run_once', [call[0] for call in self.adapter.calls])

    def test_resolved_stall_event_cannot_run_late_maintenance(self):
        self.agent.recent_executions = lambda loop=None, limit=20: [
            {'id': 'exec-new', 'state': 'SUCCEEDED', 'loop': loop,
             'created_at': 950}]
        self.agent.runtime_result = subprocess.CompletedProcess(
            ['runtime'], 0,
            stdout='{"summary":"old stall resolved","findings":[],"actions":[{"id":"scheduler.restart_registered"}]}',
            stderr='')
        result = self.executor(task(actions=['scheduler.restart_registered']))

        self.assertEqual('STALE_SIGNAL', result['actions'][0]['state'])
        self.assertEqual([], [call for call in self.adapter.calls
                              if call[0] in ('start_scheduler', 'restart_scheduler')])

    def test_duplicate_maintenance_action_id_runs_only_once(self):
        self.agent.runtime_result = subprocess.CompletedProcess(
            ['runtime'], 0,
            stdout='{"summary":"restart discover scheduler","findings":[],"actions":[{"id":"scheduler.restart_registered"},{"id":"scheduler.restart_registered"}]}',
            stderr='')
        result = self.executor(task(actions=['scheduler.restart_registered']))
        self.assertEqual(1, len([call for call in self.adapter.calls
                                 if call[0] == 'restart_scheduler']))
        self.assertEqual(['SUCCEEDED', 'DUPLICATE_ACTION'],
                         [action['state'] for action in result['actions']])

    def test_runtime_failure_returns_prepared_claim_fence_for_release(self):
        task_in = task('pr.repair', payload={'number': 42, 'reasons': ['merge_conflict']})
        prepared = {'execution': 'execution-1', 'token': 7,
                    'worktree': str(self.root / 'worktree'), 'branch': 'fix/repair-pr-42',
                    'head_ref': 'feature/example', 'task': 'PR_CONFLICT_RESOLUTION',
                    'repair_reasons': ['merge_conflict'], 'head_sha': 'abc'}
        self.agent.runtime_result = subprocess.CompletedProcess(
            ['runtime'], 3, stdout='', stderr='runtime failed')
        with patch.object(executor_mod.review_round, 'prepare_round',
                          return_value=prepared), \
                patch.object(executor_mod.trigger, 'build_round_prompt',
                             return_value='prepared handler prompt'):
            with self.assertRaises(executor_mod.WorkerTaskExecutionError) as caught:
                self.executor(task_in)
        self.assertEqual(('execution-1', 7),
                         (caught.exception.execution_id, caught.exception.token))

    def test_claim_journal_failure_reports_fence_before_runtime_start(self):
        task_in = task('pr.repair', payload={'number': 42, 'reasons': ['merge_conflict']})
        prepared = {'execution': 'execution-1', 'token': 7,
                    'worktree': str(self.root / 'worktree'),
                    'branch': 'fix/repair-pr-42', 'head_ref': 'feature/example',
                    'task': 'PR_CONFLICT_RESOLUTION',
                    'repair_reasons': ['merge_conflict'], 'head_sha': 'abc'}
        reports = []
        self.agent._record_worker_task_claim = lambda *args: (_ for _ in ()).throw(
            OSError('local journal is read-only'))
        self.agent._report_worker_task = lambda *args, **kwargs: reports.append(
            (args, kwargs))
        with patch.object(executor_mod.review_round, 'prepare_round',
                          return_value=prepared):
            with self.assertRaises(executor_mod.WorkerTaskExecutionError) as caught:
                self.executor(task_in)
        self.assertEqual(('execution-1', 7),
                         (caught.exception.execution_id, caught.exception.token))
        self.assertEqual(('execution-1', 7),
                         (reports[0][1]['execution_id'], reports[0][1]['token']))
        self.assertEqual([], self.agent.runtime_calls)


if __name__ == '__main__':
    unittest.main()
