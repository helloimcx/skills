#!/usr/bin/env python3
"""Evidence from native child tool events, without real prompts or sessions."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from codeloop.worker_task_executor import WorkerTaskExecutor, WorkerTaskExecutionError
import test_worker_task_executor as fixture

task = fixture.task


def call(tool='Agent', call_id='tool-1', parent=None, background=False):
    return {'type': 'assistant', 'parent_tool_use_id': parent,
            'message': {'content': [{'type': 'tool_use', 'name': tool,
                                     'id': call_id,
                                     'input': {'run_in_background': background, 'prompt': 'execution=execution-1'}}]}}


def result(call_id='tool-1', status='completed', error=False, parent=None):
    event = {'type': 'user', 'parent_tool_use_id': parent,
             'message': {'content': [{'type': 'tool_result',
                                      'tool_use_id': call_id, 'is_error': error,
                                      'content': 'secret-example-not-for-errors'}]}}
    if status is not None:
        event['tool_use_result'] = {'status': status, 'agentId': 'agent-1'}
    return event


def stream(*events):
    return '\n'.join(json.dumps(x) if isinstance(x, dict) else x for x in events)


class NativeEvidenceTests(unittest.TestCase):
    def evidence(self, *events, agent='claude'):
        return WorkerTaskExecutor._native_handler_call(stream(*events), agent, 'execution-1')

    def test_agent_completed_and_legacy_task_are_accepted(self):
        for tool, status in [('Agent', 'completed'), ('Task', None)]:
            with self.subTest(tool=tool):
                evidence = self.evidence(call(tool), result(status=status))
                self.assertEqual(('completed', 'tool-1'),
                                 (evidence['status'], evidence['tool_call_id']))

    def test_nested_child_cannot_stand_in_for_coordinator_call(self):
        evidence = self.evidence(call(parent='outer'), result(parent='outer'))
        self.assertEqual('not_invoked', evidence['status'])

    def test_background_or_unfinished_agent_is_not_completed(self):
        samples = [(call(background=True), result(status='running')),
                   (call(), result(status='running')),
                   (call(background=True), result()), (call(),), (call(), result(status=None))]
        for events in samples:
            with self.subTest(events=events):
                self.assertNotEqual('completed', self.evidence(*events)['status'])

    def test_result_from_child_scope_cannot_complete_parent(self):
        self.assertEqual('incomplete', self.evidence(call(), result(parent='outer'))['status'])

    def test_multiple_calls_ambiguous_but_duplicate_events_do_not_count_twice(self):
        a, b = call(), result()
        self.assertEqual('completed', self.evidence(a, a, b, b)['status'])
        self.assertEqual('ambiguous', self.evidence(a, b, call(call_id='tool-2'),
                                                    result(call_id='tool-2'))['status'])
        changed = call()
        changed['message']['content'][0]['input']['prompt'] = 'other task'
        self.assertEqual('ambiguous', self.evidence(a, changed, b)['status'])

    def test_conflicting_results_or_truncated_events_never_succeed(self):
        self.assertNotEqual('completed', self.evidence(call(), result(),
                                                       result(error=True))['status'])
        self.assertNotEqual('completed', self.evidence(call(), result(),
                                                       '{"type":"assistant"')['status'])

    def test_failure_and_unsupported_data_are_sanitized(self):
        failed = self.evidence(call(), result(error=True))
        self.assertEqual('failed', failed['status'])
        self.assertNotIn('secret-example-not-for-errors', str(failed))
        self.assertEqual('unsupported', self.evidence('{broken')['status'])

    def test_result_must_match_call_and_metadata_is_not_shared_across_blocks(self):
        self.assertEqual('incomplete', self.evidence(call(), result(call_id='other'))['status'])
        self.assertNotEqual('completed', self.evidence(result(), call())['status'])
        multi = result()
        multi['message']['content'].append({'type': 'tool_result', 'tool_use_id': 'other'})
        self.assertNotEqual('completed', self.evidence(call(), multi)['status'])

    def test_unrelated_or_missing_execution_binding_cannot_succeed(self):
        for prompt in ('', 'execution=other', 'execution=execution-1extra'):
            event = call()
            event['message']['content'][0]['input']['prompt'] = prompt
            self.assertEqual('unbound', self.evidence(event, result())['status'])

    def test_empty_stream_and_no_call_are_distinct(self):
        self.assertEqual('missing_log', self.evidence()['status'])
        self.assertEqual('not_invoked', self.evidence({'type': 'result'})['status'])

    def test_deeply_nested_json_fails_closed(self):
        bad = '{"k":' * 1500 + '0' + '}' * 1500
        self.assertEqual('unsupported', self.evidence(bad)['status'])

    def test_opencode_nested_or_missing_session_cannot_be_evidence(self):
        parent = {'type': 'text', 'sessionID': 'ses_parent'}
        nested = {'type': 'tool_use', 'sessionID': 'ses_nested',
                  'part': {'id': 'x', 'tool': 'task', 'state': {
                      'status': 'completed', 'input': {'prompt': 'execution=execution-1'}}}}
        self.assertNotEqual('completed', self.evidence(parent, nested, agent='opencode')['status'])
        nested.pop('sessionID')
        self.assertNotEqual('completed', self.evidence(nested, agent='opencode')['status'])

    def test_opencode_existing_completed_and_error(self):
        def event(status):
            return {'type': 'tool_use', 'sessionID': 'ses_parent', 'part': {'id': 'x', 'tool': 'task',
                                                  'state': {'status': status, 'input': {'prompt': 'execution=execution-1'},
                                                            'error': 'secret-example'}}}
        self.assertEqual('completed', self.evidence({'type': 'text', 'sessionID': 'ses_parent'}, event('completed'), agent='opencode')['status'])
        failure = self.evidence({'type': 'text', 'sessionID': 'ses_parent'}, event('error'), agent='opencode')
        self.assertEqual('failed', failure['status'])
        self.assertNotIn('secret-example', str(failure))

    def test_legacy_opencode_completed_event_without_input(self):
        parent = {'type': 'text', 'sessionID': 'ses_parent'}
        event = {'type': 'tool_use', 'sessionID': 'ses_parent',
                 'part': {'id': 'call_handler', 'tool': 'task',
                          'state': {'status': 'completed',
                                    'output': {'sessionId': 'ses_child'}}}}
        evidence = self.evidence(parent, event, agent='opencode')
        self.assertEqual('completed', evidence['status'])
        self.assertEqual('ses_child', evidence['session_id'])
        self.assertEqual('completed', self.evidence(event, agent='opencode')['status'])

    def test_opencode_rejects_malformed_or_unbound_present_input(self):
        parent = {'type': 'text', 'sessionID': 'ses_parent'}
        for value, expected in [('invalid', 'unsupported'),
                                ({'prompt': 'execution=other'}, 'unbound')]:
            event = {'type': 'tool_use', 'sessionID': 'ses_parent',
                     'part': {'id': 'call_handler', 'tool': 'task',
                              'state': {'status': 'completed', 'input': value}}}
            with self.subTest(value=value):
                self.assertEqual(expected, self.evidence(parent, event, agent='opencode')['status'])
                projected = WorkerTaskExecutor._repair_log_event(
                    json.dumps(event), 'opencode', 'execution-1', b'key')
                self.assertEqual(expected, self.evidence(parent, projected, agent='opencode')['status'])



class RepairEvidenceIntegrationTests(unittest.TestCase):
    def setUp(self):
        fixture.WorkerTaskExecutorTests.setUp(self)

    def tearDown(self):
        fixture.WorkerTaskExecutorTests.tearDown(self)

    def test_realistic_claude_agent_requires_both_evidence_and_cp_success(self):
        self.agent._coordinator_runtime = lambda *args: ('claude', None, ['claude', '-p'])
        self.agent.runtime_result.stdout = stream(call(), result())
        self.agent.handler_execution_state = 'SUCCEEDED'
        task_in = task('pr.repair', payload={'number': 42, 'reasons': ['merge_conflict']})
        prepared = {'execution': 'execution-1', 'token': 7,
                    'worktree': str(self.root / 'worktree'), 'branch': 'fix/repair-pr-42',
                    'head_ref': 'feature/example', 'task': 'PR_CONFLICT_RESOLUTION',
                    'repair_reasons': ['merge_conflict'], 'head_sha': 'abc'}
        with patch('codeloop.worker_task_executor.review_round.prepare_round', return_value=prepared), \
             patch('codeloop.worker_task_executor.trigger.build_round_prompt', return_value='prompt'):
            outcome = self.executor(task_in)
            self.assertEqual('execution-1', outcome['execution_id'])
            self.assertTrue(outcome['handler']['tool_call_id'].startswith('call_'))
            self.assertNotIn('secret-example-not-for-errors', json.dumps(outcome))
            for state in ['RUNNING', 'CANCELLED', None]:
                with self.subTest(state=state):
                    self.agent.handler_execution_state = state
                    with self.assertRaises(WorkerTaskExecutionError):
                        self.executor(task_in)
            self.agent.handler_execution_state = 'token=secret-cp-state'
            with self.assertRaises(WorkerTaskExecutionError) as caught:
                self.executor(task_in)
            self.assertIn('state=UNKNOWN', str(caught.exception))
            self.assertNotIn('secret-cp-state', str(caught.exception))

    def test_mismatched_cp_identity_and_runtime_error_never_succeed_or_leak(self):
        self.agent._coordinator_runtime = lambda *args: ('claude', None, ['claude', '-p'])
        self.agent.runtime_result.stdout = stream(call(), result())
        task_in = task('pr.repair', payload={'number': 42, 'reasons': ['merge_conflict']})
        prepared = {'execution': 'execution-1', 'token': 7,
                    'worktree': str(self.root / 'worktree'), 'branch': 'fix/repair-pr-42',
                    'head_ref': 'feature/example', 'task': 'PR_CONFLICT_RESOLUTION',
                    'repair_reasons': ['merge_conflict'], 'head_sha': 'abc'}
        with patch('codeloop.worker_task_executor.review_round.prepare_round', return_value=prepared), \
             patch('codeloop.worker_task_executor.trigger.build_round_prompt', return_value='prompt'):
            self.agent.execution_status = lambda execution: {'id': 'other', 'state': 'SUCCEEDED'}
            with self.assertRaises(WorkerTaskExecutionError) as caught:
                self.executor(task_in)
            self.assertIn('HANDLER_EXECUTION', str(caught.exception))
            self.agent.execution_status = lambda execution: (_ for _ in ()).throw(
                RuntimeError('secret-cp-credential'))
            with self.assertRaises(WorkerTaskExecutionError) as caught:
                self.executor(task_in)
            self.assertNotIn('secret-cp-credential', str(caught.exception))
            self.agent.runtime_result.returncode = 1
            self.agent.runtime_result.stdout = 'secret-runtime-output'
            with self.assertRaises(WorkerTaskExecutionError) as caught:
                self.executor(task_in)
            self.assertNotIn('secret-runtime-output', str(caught.exception))
            self.assertEqual(('execution-1', 7),
                             (caught.exception.execution_id, caught.exception.token))

    def test_failures_report_stable_reason_and_preserve_fence(self):
        self.agent._coordinator_runtime = lambda *args: ('claude', None, ['claude', '-p'])
        self.agent.handler_execution_state = 'SUCCEEDED'
        task_in = task('pr.repair', payload={'number': 42, 'reasons': ['merge_conflict']})
        prepared = {'execution': 'execution-1', 'token': 7,
                    'worktree': str(self.root / 'worktree'), 'branch': 'fix/repair-pr-42',
                    'head_ref': 'feature/example', 'task': 'PR_CONFLICT_RESOLUTION',
                    'repair_reasons': ['merge_conflict'], 'head_sha': 'abc'}
        cases = [(stream({'type': 'result'}), 'HANDLER_CHILD_UNCONFIRMED', 'not_invoked'),
                 (stream(call()), 'HANDLER_CHILD_UNCONFIRMED', 'incomplete'),
                 (stream(call(), result(error=True)), 'HANDLER_CHILD_FAILED', 'failed'),
                 (stream(call(), result(), call(call_id='other')), 'HANDLER_CHILD_UNCONFIRMED', 'ambiguous'),
                 ('{broken', 'HANDLER_CHILD_UNCONFIRMED', 'unsupported'),
                 ('', 'HANDLER_CHILD_UNCONFIRMED', 'missing_log')]
        with patch('codeloop.worker_task_executor.review_round.prepare_round', return_value=prepared), \
             patch('codeloop.worker_task_executor.trigger.build_round_prompt', return_value='prompt'):
            for output, code, reason in cases:
                with self.subTest(reason=reason):
                    self.agent.runtime_result.stdout = output
                    with self.assertRaises(WorkerTaskExecutionError) as caught:
                        self.executor(task_in)
                    message = str(caught.exception)
                    self.assertIn(code, message)
                    self.assertIn('reason='+reason, message)
                    self.assertIn('execution-1', message)
                    self.assertNotIn('secret-example-not-for-errors', message)
                    self.assertEqual(('execution-1', 7),
                                     (caught.exception.execution_id, caught.exception.token))

    def test_runtime_log_is_private_and_redacts_claim_token(self):
        import os
        import stat
        self.agent._coordinator_runtime = lambda *args: ('claude', None, ['claude', '-p'])
        self.agent.handler_execution_state = 'SUCCEEDED'
        event = call()
        event['message']['content'][0]['input']['prompt'] += ' token=7 --token 7'
        self.agent.runtime_result.stdout = stream(event, result())
        task_in = task('pr.repair', payload={'number': 42, 'reasons': ['merge_conflict']})
        prepared = {'execution': 'execution-1', 'token': 7,
                    'worktree': str(self.root / 'worktree'), 'branch': 'fix/repair-pr-42',
                    'head_ref': 'feature/example', 'task': 'PR_CONFLICT_RESOLUTION',
                    'repair_reasons': ['merge_conflict'], 'head_sha': 'abc'}
        with patch('codeloop.worker_task_executor.review_round.prepare_round', return_value=prepared), \
             patch('codeloop.worker_task_executor.trigger.build_round_prompt', return_value='prompt'):
            outcome = self.executor(task_in)
        log = Path(outcome['log_path'])
        self.assertNotIn('token=7', log.read_text())
        self.assertNotIn('--token 7', log.read_text())
        self.assertNotIn('secret-example-not-for-errors', log.read_text())
        if os.name != 'nt':
            self.assertEqual(0o600, stat.S_IMODE(log.stat().st_mode))

    def test_real_subprocess_streams_only_safe_evidence(self):
        import subprocess
        self.agent.agent_popen_fn = subprocess.Popen
        payload = stream(call(), result()) + '\n'
        code = ('import sys; sys.stdin.buffer.read(); '
                'sys.stdout.write(' + repr(payload) + '); sys.stdout.flush()')
        self.agent._coordinator_runtime = lambda *args: (
            'claude', None, [sys.executable, '-c', code])
        self.agent.handler_execution_state = 'SUCCEEDED'
        task_in = task('pr.repair', payload={'number': 42, 'reasons': ['merge_conflict']})
        prepared = {'execution': 'execution-1', 'token': 7,
                    'worktree': str(self.root), 'branch': 'fix/repair-pr-42',
                    'head_ref': 'feature/example', 'task': 'PR_CONFLICT_RESOLUTION',
                    'repair_reasons': ['merge_conflict'], 'head_sha': 'abc'}
        with patch('codeloop.worker_task_executor.review_round.prepare_round', return_value=prepared), \
             patch('codeloop.worker_task_executor.trigger.build_round_prompt', return_value='token=7'):
            outcome = self.executor(task_in)
        self.assertEqual('execution-1', outcome['execution_id'])
        log = Path(outcome['log_path']).read_text()
        self.assertNotIn('secret-example-not-for-errors', log)
        self.assertNotIn('token=7', log)
        self.assertEqual('completed', WorkerTaskExecutor._native_handler_call(
            log, 'claude', 'execution-1')['status'])

    def test_unsupported_deep_runtime_output_keeps_fence(self):
        self.agent._coordinator_runtime = lambda *args: ('claude', None, ['claude', '-p'])
        self.agent.runtime_result.stdout = '{"k":' * 1500 + '0' + '}' * 1500
        task_in = task('pr.repair', payload={'number': 42, 'reasons': ['merge_conflict']})
        prepared = {'execution': 'execution-1', 'token': 7,
                    'worktree': str(self.root / 'worktree'), 'branch': 'fix/repair-pr-42',
                    'head_ref': 'feature/example', 'task': 'PR_CONFLICT_RESOLUTION',
                    'repair_reasons': ['merge_conflict'], 'head_sha': 'abc'}
        with patch('codeloop.worker_task_executor.review_round.prepare_round', return_value=prepared), \
             patch('codeloop.worker_task_executor.trigger.build_round_prompt', return_value='prompt'):
            with self.assertRaises(WorkerTaskExecutionError) as caught:
                self.executor(task_in)
        self.assertIn('reason=unsupported', str(caught.exception))
        self.assertEqual(('execution-1', 7), (caught.exception.execution_id, caught.exception.token))

    def test_conflicting_duplicate_input_does_not_become_one_safe_call(self):
        self.agent._coordinator_runtime = lambda *args: ('claude', None, ['claude', '-p'])
        first = call()
        second = call()
        second['message']['content'][0]['input']['prompt'] += ' unrelated extra'
        self.agent.runtime_result.stdout = stream(first, second, result())
        task_in = task('pr.repair', payload={'number': 42, 'reasons': ['merge_conflict']})
        prepared = {'execution': 'execution-1', 'token': 7,
                    'worktree': str(self.root / 'worktree'), 'branch': 'fix/repair-pr-42',
                    'head_ref': 'feature/example', 'task': 'PR_CONFLICT_RESOLUTION',
                    'repair_reasons': ['merge_conflict'], 'head_sha': 'abc'}
        with patch('codeloop.worker_task_executor.review_round.prepare_round', return_value=prepared), \
             patch('codeloop.worker_task_executor.trigger.build_round_prompt', return_value='prompt'):
            with self.assertRaises(WorkerTaskExecutionError) as caught:
                self.executor(task_in)
        self.assertIn('reason=ambiguous', str(caught.exception))
        log = (self.root / 'worker-task-logs' / 'task-1.log').read_text()
        self.assertNotIn('unrelated extra', log)

    def test_journal_failure_after_spawn_stops_child(self):
        processes = []
        def popen(argv, **kwargs):
            process = fixture._Process(1234, 0)
            processes.append(process)
            return process
        self.agent.agent_popen_fn = popen
        self.agent._record_worker_task_runtime = lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError('secret-journal-detail'))
        task_in = task('pr.repair', payload={'number': 42, 'reasons': ['merge_conflict']})
        prepared = {'execution': 'execution-1', 'token': 7,
                    'worktree': str(self.root / 'worktree'), 'branch': 'fix/repair-pr-42',
                    'head_ref': 'feature/example', 'task': 'PR_CONFLICT_RESOLUTION',
                    'repair_reasons': ['merge_conflict'], 'head_sha': 'abc'}
        with patch('codeloop.worker_task_executor.review_round.prepare_round', return_value=prepared), \
             patch('codeloop.worker_task_executor.trigger.build_round_prompt', return_value='prompt'):
            with self.assertRaises(WorkerTaskExecutionError) as caught:
                self.executor(task_in)
        self.assertEqual([], processes,
                         'Coordinator must not start before the runtime is journaled')
        self.assertNotIn('secret-journal-detail', str(caught.exception))
        self.assertEqual(('execution-1', 7), (caught.exception.execution_id, caught.exception.token))

    def test_claim_journal_failure_does_not_expose_exception(self):
        self.agent._record_worker_task_claim = lambda *args: (_ for _ in ()).throw(
            OSError('token=secret-journal-value'))
        reports = []
        self.agent._report_worker_task = lambda *args, **kwargs: reports.append(kwargs)
        prepared = {'execution': 'execution-1', 'token': 7,
                    'worktree': str(self.root / 'worktree')}
        with patch('codeloop.worker_task_executor.review_round.prepare_round', return_value=prepared), \
             patch('codeloop.worker_task_executor.review_round.remove_worktree_quietly'):
            with self.assertRaises(WorkerTaskExecutionError) as caught:
                self.executor(task('pr.repair', payload={'number': 42, 'reasons': ['merge_conflict']}))
        self.assertNotIn('secret-journal-value', str(caught.exception))
        self.assertNotIn('secret-journal-value', str(reports))
        self.assertIn('WORKER_TASK_JOURNAL_FAILED', str(caught.exception))
