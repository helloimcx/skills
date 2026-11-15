#!/usr/bin/env python3
"""Process-level checks for private repair logs across worker failure."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from codeloop.worker_task_executor import WorkerTaskExecutor


@unittest.skipIf(os.name == 'nt', 'POSIX process-death scenario')
class DetachedSinkTests(unittest.TestCase):
    def test_runtime_survives_worker_death_and_log_never_contains_tool_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker_pid = root / 'runtime.pid'
            completed = root / 'runtime.completed'
            event = {'type': 'assistant', 'parent_tool_use_id': None,
                     'message': {'content': [{'type': 'tool_use', 'name': 'Agent',
                         'id': 'tool-1', 'input': {'prompt':
                             'execution=execution-1 token=7 secret-example'}}]}}
            result = {'type': 'user', 'parent_tool_use_id': None,
                      'message': {'content': [{'type': 'tool_result',
                          'tool_use_id': 'tool-1', 'content': 'secret-example'}]},
                      'tool_use_result': {'status': 'completed'}}
            payload = json.dumps(event) + '\n' + json.dumps(result) + '\n'
            child = ('import sys,time,pathlib; time.sleep(0.7); '
                     'sys.stdout.write(' + repr(payload) + '); sys.stdout.flush(); '
                     'pathlib.Path(' + repr(str(completed)) + ').write_text("done")')
            supervisor = '''import subprocess,sys
from pathlib import Path
from codeloop.worker_task_executor import WorkerTaskExecutor
root=Path(sys.argv[1]); child=sys.argv[2]
class Agent:
    db_path=str(root/'dashboard.sqlite')
    agent_popen_fn=staticmethod(subprocess.Popen)
    def _record_worker_task_runtime(self, task_id, pid, log_path, **kw):
        (root/'runtime.pid').write_text(str(pid))
executor=WorkerTaskExecutor(Agent())
executor._run_session({'id':'task-1','task_type':'pr.repair'}, 'claude',
    [sys.executable,'-c',child], 'token=7', str(root),
    execution_id='execution-1', token=7)
'''
            proc = subprocess.Popen([sys.executable, '-c', supervisor, str(root), child],
                                    cwd=str(ROOT), stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 8
                while not worker_pid.exists() and time.monotonic() < deadline:
                    if proc.poll() is not None:
                        self.fail('worker exited before recording coordinator PID')
                    time.sleep(0.02)
                self.assertTrue(worker_pid.exists())
                proc.kill()
                proc.wait(timeout=5)
                deadline = time.monotonic() + 8
                while not completed.exists() and time.monotonic() < deadline:
                    time.sleep(0.03)
                self.assertTrue(completed.exists(), 'coordinator could not write after worker death')
                log_path = root / 'worker-task-logs' / 'task-1.log'
                deadline = time.monotonic() + 5
                log = ''
                while time.monotonic() < deadline:
                    log = log_path.read_text() if log_path.exists() else ''
                    if WorkerTaskExecutor._native_handler_call(log, 'claude',
                                                                'execution-1')['status'] == 'completed':
                        break
                    time.sleep(0.03)
                self.assertEqual('completed', WorkerTaskExecutor._native_handler_call(
                    log, 'claude', 'execution-1')['status'])
                self.assertNotIn('token=7', log)
                self.assertNotIn('secret-example', log)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)

    def test_supervisor_reads_utf8_stream_with_non_utf8_worker_environment(self):
        env = dict(os.environ, PYTHONIOENCODING='ascii')
        payload = '{"type":"result","result":"中文"}\n'.encode('utf-8')
        code = ('import sys;sys.stdin.buffer.read();sys.stdout.buffer.write(' +
                repr(payload) + ');sys.stdout.flush()')
        child = ('import os,subprocess,sys;env=dict(os.environ,PYTHONIOENCODING="ascii");'
                 'subprocess.run([sys.executable,"-c",' + repr(code) + '],env=env,check=True)')
        class Agent:
            db_path = str(Path(tempfile.gettempdir()) / 'dashboard.sqlite')
            agent_popen_fn = staticmethod(subprocess.Popen)
        with tempfile.TemporaryDirectory() as tmp:
            Agent.db_path = str(Path(tmp) / 'dashboard.sqlite')
            executor = WorkerTaskExecutor(Agent())
            stdout, _ = executor._run_session(
                {'id': 'task-unicode', 'task_type': 'pr.repair'}, 'claude',
                [sys.executable, '-c', child], 'prompt', tmp,
                execution_id='execution-1', token=7)
        self.assertIn('session_marker', stdout)
        self.assertNotIn('中文', stdout)

    def test_background_process_holding_stdout_does_not_fail_completed_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child_done = root / 'child.done'
            class Agent:
                db_path = str(root / 'dashboard.sqlite')
                agent_popen_fn = staticmethod(subprocess.Popen)
                runtime_pid = None
                def _record_worker_task_runtime(self, task_id, pid, log_path, **kwargs):
                    self.runtime_pid = pid
            child = ('import pathlib,time; time.sleep(3); print("background",flush=True); '
                     'pathlib.Path(' + repr(str(child_done)) + ').write_text("done")')
            code = ('import subprocess,sys; subprocess.Popen([sys.executable,"-c",' +
                    repr(child) + ']); print("coordinator",flush=True)')
            start = time.monotonic()
            agent = Agent()
            WorkerTaskExecutor(agent)._run_session(
                {'id': 'task-background', 'task_type': 'pr.repair'}, 'claude',
                [sys.executable, '-c', code], 'prompt', str(root),
                execution_id='execution-1', token=7)
            self.assertLess(time.monotonic() - start, 2.5)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(agent.runtime_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            else:
                self.fail('journaled sink should exit after reporting the direct runtime')
            deadline = time.monotonic() + 6
            while not child_done.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(child_done.exists(), 'background output must remain drainable')

    def test_large_prompt_timeout_when_runtime_does_not_read_stdin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            completed = root / 'runtime.completed'
            class Agent:
                db_path = str(root / 'dashboard.sqlite')
                agent_popen_fn = staticmethod(subprocess.Popen)
            code = ('import pathlib,time;time.sleep(2);'
                    'pathlib.Path(' + repr(str(completed)) + ').write_text("done")')
            started = time.monotonic()
            with patch('codeloop.worker_task_executor.RUNTIME_TIMEOUT_SECONDS', 0.3):
                with self.assertRaisesRegex(ValueError, 'COORDINATOR_RUNTIME_TIMEOUT'):
                    WorkerTaskExecutor(Agent())._run_session(
                        {'id': 'task-large-prompt', 'task_type': 'pr.repair'}, 'claude',
                        [sys.executable, '-c', code], 'x' * (1024 * 1024), str(root),
                        execution_id='execution-1', token=7)
            self.assertLess(time.monotonic() - started, 1.8)
            self.assertFalse(completed.exists())


class WindowsTimeoutTests(unittest.TestCase):
    def test_timeout_kills_direct_runtime_on_windows(self):
        import io
        from codeloop import worker_task_supervisor as supervisor
        class Runtime:
            pid = 1234
            stdin = io.BytesIO()
            stdout = io.BytesIO(b'')
            killed = False
            waits = 0
            def wait(self, timeout=None):
                self.waits += 1
                if self.waits == 1:
                    raise subprocess.TimeoutExpired('runtime', timeout)
                return -9
            def kill(self):
                self.killed = True
        runtime = Runtime()
        with tempfile.TemporaryDirectory() as tmp:
            config = {'log_path': str(Path(tmp) / 'task.log'), 'cwd': tmp,
                      'argv': ['runtime'], 'env': None, 'prompt': '',
                      'execution_id': 'execution-1', 'agent_name': 'claude', 'timeout': 0.01}
            with patch.object(supervisor.os, 'name', 'nt'), \
                 patch.object(supervisor.subprocess, 'Popen', return_value=runtime), \
                 patch.object(supervisor.signal, 'signal'):
                status = supervisor._run(config)
        self.assertEqual(124, status)
        self.assertTrue(runtime.killed)
        self.assertEqual(2, runtime.waits)
