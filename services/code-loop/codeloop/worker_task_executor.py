"""Execute typed Operator tasks on the worker that owns their project.

LLM sessions receive a fixed prompt and scoped runtime profile. They can ask
for a maintenance action by ID; the worker validates the ID, rechecks local
registration and execution state, and runs the corresponding fixed adapter.
No model-produced command or target reaches a subprocess.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time

from . import review_round, trigger
from .cli import resolve_command
from .cp import runtime as cp_runtime
from .worker_task_protocol import MAINTENANCE_ACTIONS

RUNTIME_TIMEOUT_SECONDS = 4 * 60 * 60
MAX_LOG_CHARS = 16000
RUNTIME_STDIN_MARKER = 'Read the complete task instructions from standard input.'
_SAFE_ID = re.compile(r'[^A-Za-z0-9_-]')
_SESSION_ID = re.compile(r'\bses_[A-Za-z0-9_-]+\b')


class WorkerTaskExecutionError(RuntimeError):
    """A failure after a PR claim carries its fencing token for release."""

    def __init__(self, message, execution_id=None, token=None):
        super().__init__(message)
        self.execution_id = execution_id
        self.token = token


class WindowsMaintenanceAdapter:
    """Fixed Task Scheduler and Windows service calls for registered targets."""

    _QUERY_TASK = r"""
$ErrorActionPreference = 'Stop'
$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$folder = $service.GetFolder($env:CODE_LOOP_TASK_PATH)
$task = $folder.GetTask($env:CODE_LOOP_TASK_NAME)
$info = Get-ScheduledTaskInfo -TaskName $env:CODE_LOOP_TASK_NAME -TaskPath $env:CODE_LOOP_TASK_PATH
@{exists=$true; enabled=[bool]$task.Enabled; state=[int]$task.State;
  lastRunTime=$info.LastRunTime.ToUniversalTime().ToString('o');
  lastTaskResult=[int]$info.LastTaskResult} | ConvertTo-Json -Compress
""".strip()
    _START_TASK = r"""
$ErrorActionPreference = 'Stop'
$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$task = $service.GetFolder($env:CODE_LOOP_TASK_PATH).GetTask($env:CODE_LOOP_TASK_NAME)
if (-not $task.Enabled) { throw 'REGISTERED_TASK_DISABLED' }
$task.Run($null) | Out-Null
@{requested=$true} | ConvertTo-Json -Compress
""".strip()
    _RESTART_TASK = r"""
$ErrorActionPreference = 'Stop'
$service = New-Object -ComObject 'Schedule.Service'
$service.Connect()
$task = $service.GetFolder($env:CODE_LOOP_TASK_PATH).GetTask($env:CODE_LOOP_TASK_NAME)
if (-not $task.Enabled) { throw 'REGISTERED_TASK_DISABLED' }
if ($task.State -eq 4) {
  $task.Stop(0)
  $until = (Get-Date).AddSeconds(20)
  while ($task.State -eq 4 -and (Get-Date) -lt $until) { Start-Sleep -Milliseconds 250 }
  if ($task.State -eq 4) { throw 'REGISTERED_TASK_DID_NOT_STOP' }
}
$task.Run($null) | Out-Null
@{requested=$true} | ConvertTo-Json -Compress
""".strip()
    _QUERY_SERVICE = r"""
$ErrorActionPreference = 'Stop'
$service = Get-Service -Name $env:CODE_LOOP_SERVICE_NAME -ErrorAction Stop
@{exists=$true; state=$service.Status.ToString()} | ConvertTo-Json -Compress
""".strip()
    _START_SERVICE = r"""
$ErrorActionPreference = 'Stop'
Start-Service -Name $env:CODE_LOOP_SERVICE_NAME -ErrorAction Stop
@{requested=$true} | ConvertTo-Json -Compress
""".strip()

    def __init__(self, run_fn=None, platform=None):
        self.run_fn = run_fn or subprocess.run
        self.platform = os.name if platform is None else platform

    def _powershell(self, script, env):
        if self.platform != 'nt':
            raise ValueError('MAINTENANCE_PLATFORM_UNSUPPORTED: windows Task Scheduler/service')
        child_env = os.environ.copy()
        child_env.update(env)
        try:
            done = self.run_fn(['powershell.exe', '-NoLogo', '-NoProfile',
                                '-NonInteractive', '-Command', script],
                               capture_output=True, text=True, timeout=30,
                               env=child_env)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError('MAINTENANCE_COMMAND_FAILED: %s: %s'
                             % (type(exc).__name__, exc)) from None
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or '').strip()[:500]
            raise ValueError('MAINTENANCE_COMMAND_FAILED: %s' % detail)
        try:
            data = json.loads(done.stdout or '{}')
        except ValueError:
            raise ValueError('MAINTENANCE_RESULT_INVALID') from None
        if not isinstance(data, dict):
            raise ValueError('MAINTENANCE_RESULT_INVALID')
        return data

    @staticmethod
    def _task_env(registration):
        if not isinstance(registration, dict):
            raise ValueError('SCHEDULER_TASK_NOT_REGISTERED')
        name, path = registration.get('name'), registration.get('path', '\\')
        if (not isinstance(name, str) or not name.strip()
                or not isinstance(path, str) or not path.startswith('\\')):
            raise ValueError('SCHEDULER_TASK_REGISTRATION_INVALID')
        return {'CODE_LOOP_TASK_NAME': name, 'CODE_LOOP_TASK_PATH': path}

    def query_scheduler(self, registration):
        return self._powershell(self._QUERY_TASK, self._task_env(registration))

    def start_scheduler(self, registration):
        return self._powershell(self._START_TASK, self._task_env(registration))

    def restart_scheduler(self, registration):
        return self._powershell(self._RESTART_TASK, self._task_env(registration))

    @staticmethod
    def _service_env(registration):
        if not isinstance(registration, str) or not re.fullmatch(
                r'[A-Za-z0-9_.-]{1,128}', registration):
            raise ValueError('SERVICE_NOT_REGISTERED')
        return {'CODE_LOOP_SERVICE_NAME': registration}

    def query_service(self, registration):
        return self._powershell(self._QUERY_SERVICE, self._service_env(registration))

    def start_service(self, registration):
        return self._powershell(self._START_SERVICE, self._service_env(registration))


class WorkerTaskExecutor:
    def __init__(self, agent, maintenance_adapter=None, watchdog_log=None,
                 clock=time.time):
        self.agent = agent
        self.maintenance = maintenance_adapter or WindowsMaintenanceAdapter()
        self.watchdog_log = Path(watchdog_log or
                                 (cp_runtime.data_home() / 'logs' / 'watchdog.jsonl'))
        self.clock = clock

    def __call__(self, task):
        task_type = task.get('task_type')
        if task_type == 'pr.repair':
            return self._repair_pr(task)
        if task_type == 'worker.project_diagnose':
            return self._diagnose_project(task)
        raise ValueError('WORKER_TASK_TYPE_UNKNOWN: %s' % task_type)

    def _context(self, task, require_handler=False):
        project = task.get('project')
        row = self.agent._task_registration(project, require_handler=require_handler)
        if row is None:
            raise ValueError('TASK_PROJECT_REQUIRED')
        cfg = self.agent.cfg_factory(project, row)
        agent_name, model, argv = self.agent._coordinator_runtime(
            row, cfg, task['task_type'])
        return project, row, cfg, agent_name, model, argv

    def _runtime_environment(self, agent_name, task_type):
        runner = self.agent_runners.REGISTRY.get(agent_name)
        provider = getattr(runner, 'coordinator_environment', None)
        return provider(task_type) if callable(provider) else None

    @property
    def agent_runners(self):
        from . import agent_runners
        return agent_runners

    def _runtime_log(self, task_id):
        safe = _SAFE_ID.sub('_', str(task_id))[:80] or 'task'
        path = Path(self.agent.db_path).expanduser().with_name('worker-task-logs')
        path.mkdir(parents=True, exist_ok=True)
        return path / (safe + '.log')

    def _run_session(self, task, agent_name, argv, prompt, cwd,
                     execution_id=None, token=None, worktree=None):
        log_path = self._runtime_log(task['id'])
        prompt_path = log_path.with_suffix('.prompt')
        env = self._runtime_environment(agent_name, task['task_type'])
        spawn_kwargs = {'cwd': str(cwd), 'stdin': None, 'stdout': None,
                        'stderr': subprocess.STDOUT, 'env': env}
        if os.name == 'nt':
            spawn_kwargs['creationflags'] = (
                getattr(subprocess, 'DETACHED_PROCESS', 0)
                | getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        else:
            spawn_kwargs['start_new_session'] = True
        try:
            prompt_path.write_text(prompt, encoding='utf-8')
            try:
                os.chmod(prompt_path, 0o600)
            except OSError:
                pass
            with prompt_path.open('rb') as prompt_input, \
                    log_path.open('w', encoding='utf-8') as output:
                spawn_kwargs['stdin'] = prompt_input
                spawn_kwargs['stdout'] = output
                proc = self.agent.agent_popen_fn(
                    list(argv) + [RUNTIME_STDIN_MARKER], **spawn_kwargs)
                record_runtime = getattr(
                    self.agent, '_record_worker_task_runtime', None)
                if callable(record_runtime):
                    record_runtime(task['id'], proc.pid, str(log_path),
                                   execution_id=execution_id, token=token,
                                   worktree=worktree)
                try:
                    returncode = proc.wait(timeout=RUNTIME_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    raise ValueError('COORDINATOR_RUNTIME_TIMEOUT') from None
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            detail = '%s: %s' % (type(exc).__name__, exc)
            try:
                existing = log_path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                existing = ''
            log_path.write_text((existing + '\n' + detail)[-MAX_LOG_CHARS:],
                                encoding='utf-8')
            raise ValueError('COORDINATOR_RUNTIME_FAILED: ' + detail) from None
        finally:
            try:
                prompt_path.unlink()
            except OSError:
                pass
        stdout = log_path.read_text(encoding='utf-8', errors='replace')
        if returncode != 0:
            raise ValueError('COORDINATOR_RUNTIME_FAILED: rc=%s; %s'
                             % (returncode, stdout[-800:]))
        return stdout, str(log_path)

    @staticmethod
    def _output_text(stdout):
        parts = []

        def visit(value):
            if isinstance(value, dict):
                for key in ('result', 'text', 'content', 'message'):
                    item = value.get(key)
                    if isinstance(item, str) and item:
                        parts.append(item)
                    elif isinstance(item, (dict, list)):
                        visit(item)
                for key in ('part', 'parts', 'data'):
                    if key in value:
                        visit(value[key])
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        try:
            visit(json.loads(stdout))
        except ValueError:
            for line in (stdout or '').splitlines():
                try:
                    visit(json.loads(line))
                except ValueError:
                    parts.append(line)
        return '\n'.join(part for part in parts if part).strip() or (stdout or '').strip()

    @staticmethod
    def _session_ids(stdout, text=''):
        return list(dict.fromkeys(_SESSION_ID.findall((stdout or '') + '\n' + (text or ''))))

    @staticmethod
    def _native_handler_call(stdout, agent_name):
        """Return evidence of a completed native child tool call from runtime events."""
        events = []
        for line in (stdout or '').splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                events.append(value)

        def child_session(value):
            if isinstance(value, dict):
                for key in ('sessionID', 'sessionId', 'session_id',
                            'childSessionId', 'child_session_id'):
                    candidate = value.get(key)
                    if isinstance(candidate, str) and candidate.startswith('ses_'):
                        return candidate
                for item in value.values():
                    found = child_session(item)
                    if found:
                        return found
            elif isinstance(value, list):
                for item in value:
                    found = child_session(item)
                    if found:
                        return found
            return None

        if agent_name == 'opencode':
            calls = {}
            for event in events:
                if event.get('type') != 'tool_use':
                    continue
                part = event.get('part') if isinstance(event.get('part'), dict) else {}
                if str(part.get('tool') or '').lower() not in ('task', 'subagent'):
                    continue
                state = part.get('state') if isinstance(part.get('state'), dict) else {}
                call_id = part.get('callID') or part.get('id') or state.get('callID')
                if not call_id:
                    continue
                status = str(state.get('status') or '').lower()
                calls[str(call_id)] = {
                    'tool_call_id': str(call_id),
                    'session_id': child_session(state),
                    'status': status,
                    'error': str(state.get('error') or 'native subagent tool failed')
                    if status == 'error' else None}
            completed = [call for call in calls.values()
                         if call['status'] in ('completed', 'complete', 'success')]
            if completed:
                return completed[-1]
            errors = [call for call in calls.values() if call['error']]
            return errors[-1] if errors else None

        if agent_name == 'claude':
            tool_calls = {}
            tool_results = {}
            for event in events:
                message = event.get('message')
                content = message.get('content') if isinstance(message, dict) else None
                if not isinstance(content, list):
                    continue
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if (event.get('type') == 'assistant'
                            and part.get('type') == 'tool_use'
                            and str(part.get('name') or '').lower() == 'task'):
                        tool_id = part.get('id')
                        if isinstance(tool_id, str) and tool_id:
                            tool_calls[tool_id] = child_session(part.get('input'))
                    elif event.get('type') == 'user' and part.get('type') == 'tool_result':
                        tool_id = part.get('tool_use_id')
                        if isinstance(tool_id, str) and tool_id:
                            tool_results[tool_id] = part
            for tool_id, session_id in tool_calls.items():
                result = tool_results.get(tool_id)
                if result is None:
                    continue
                if result.get('is_error'):
                    return {'tool_call_id': tool_id, 'session_id': session_id,
                            'error': str(result.get('content') or 'native Task tool failed')}
                return {'tool_call_id': tool_id, 'session_id': session_id,
                        'status': 'completed', 'error': None}
        return None

    def _repair_pr(self, task):
        project, row, cfg, agent_name, _model, argv = self._context(
            task, require_handler=True)
        number = task['payload']['number']
        coordinator = review_round.RemoteCoordinator(
            self.agent.worker_factory(cfg), loop='handler')
        prepared = review_round.prepare_round(
            coordinator, cfg, number, git_fn=self.agent.git_fn)
        record_claim = getattr(self.agent, '_record_worker_task_claim', None)
        if callable(record_claim):
            try:
                record_claim(task['id'], prepared['execution'], prepared['token'],
                             prepared['worktree'])
            except Exception as exc:
                review_round.remove_worktree_quietly(
                    cfg, prepared.get('worktree'), self.agent.git_fn)
                reason = 'WORKER_TASK_JOURNAL_FAILED: %s: %s' % (
                    type(exc).__name__, exc)
                report = getattr(self.agent, '_report_worker_task', None)
                if callable(report):
                    report(task['id'], 'FAILED',
                           execution_id=prepared.get('execution'),
                           token=prepared.get('token'), fail_reason=reason)
                raise WorkerTaskExecutionError(
                    reason, prepared.get('execution'), prepared.get('token')) from None
        payload = task['payload']
        stale_facts = []
        # The base branch may advance while a conflict task is queued. The
        # claimed item is the current merge target; only a changed PR head or
        # target branch makes the original repair event stale.
        for field in ('head_sha', 'base_ref'):
            expected = payload.get(field)
            actual = prepared.get(field)
            if expected is not None and actual != expected:
                stale_facts.append(field)
        if stale_facts:
            review_round.remove_worktree_quietly(
                cfg, prepared.get('worktree'), self.agent.git_fn)
            raise WorkerTaskExecutionError(
                'STALE_TASK_SOURCE: ' + ','.join(stale_facts),
                prepared.get('execution'), prepared.get('token'))
        try:
            prompt = trigger.build_round_prompt('handler', row, number, prepared)
            coordinator_prompt = (
                'You are the Worker Coordinator for task %s. Delegate all repository '
                'work to exactly one independent Handler subagent using this runtime\'s '
                'native subagent/Task capability. Do not inspect or edit project files, '
                'run shell commands, or perform Handler steps yourself. Give the child '
                'the complete prepared prompt below, wait for it to finish, and return '
                'JSON with summary and handler_session_id. The child owns the prepared '
                'worktree and must follow the existing claim, verify, report, edit_pr, '
                'and publish contract.\n\n<handler_prompt>\n%s\n</handler_prompt>'
                % (task['id'], prompt))
            stdout, log_path = self._run_session(
                task, agent_name, argv, coordinator_prompt, prepared['worktree'],
                execution_id=prepared['execution'], token=prepared['token'],
                worktree=prepared['worktree'])
        except Exception as exc:
            review_round.remove_worktree_quietly(
                cfg, prepared.get('worktree'), self.agent.git_fn)
            raise WorkerTaskExecutionError(
                str(exc), prepared.get('execution'), prepared.get('token')) from None
        output = self._output_text(stdout)
        sessions = self._session_ids(stdout, output)
        handler_call = self._native_handler_call(stdout, agent_name)
        if not output or handler_call is None:
            raise WorkerTaskExecutionError(
                'HANDLER_CHILD_UNCONFIRMED: runtime output contains no completed native Task call',
                prepared.get('execution'), prepared.get('token'))
        if handler_call.get('error'):
            raise WorkerTaskExecutionError(
                'HANDLER_CHILD_FAILED: ' + handler_call['error'],
                prepared.get('execution'), prepared.get('token'))
        try:
            handler_execution = coordinator.worker.execution_status(
                prepared['execution'])
        except Exception as exc:
            raise WorkerTaskExecutionError(
                'HANDLER_EXECUTION_STATUS_UNAVAILABLE: %s: %s'
                % (type(exc).__name__, exc),
                prepared.get('execution'), prepared.get('token')) from None
        if not handler_execution or handler_execution.get('state') != 'SUCCEEDED':
            state = (handler_execution or {}).get('state', 'MISSING')
            raise WorkerTaskExecutionError(
                'HANDLER_EXECUTION_NOT_SUCCEEDED: state=%s' % state,
                prepared.get('execution'), prepared.get('token'))
        return {'summary': output[-4000:], 'execution_id': prepared['execution'],
                'session_id': sessions[0] if sessions else None,
                'handler': {'execution_id': prepared['execution'],
                            'session_id': handler_call.get('session_id'),
                            'tool_call_id': handler_call['tool_call_id'],
                            'role': 'Handler'},
                'log_path': log_path, 'worktree': prepared['worktree']}

    def _read_watchdog(self, project, limit=30):
        if not self.watchdog_log.is_file():
            return {'status': 'unknown', 'path': str(self.watchdog_log), 'entries': []}
        try:
            lines = self.watchdog_log.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError as exc:
            return {'status': 'unknown', 'path': str(self.watchdog_log),
                    'error': type(exc).__name__, 'entries': []}
        entries = []
        for line in lines[-limit:]:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not project or not item.get('org_repo') or item.get('org_repo') == project:
                entries.append({key: _bounded_value(value)
                                for key, value in item.items()})
        return {'status': 'observed', 'path': str(self.watchdog_log),
                'entries': entries}

    def _collect_evidence(self, task, row, cfg, worker):
        now = self.clock()
        evidence = {
            'observed_at': now,
            'worker': {'hostname': socket.gethostname(), 'pid': os.getpid(),
                       'channel_connected': bool(getattr(self.agent,
                                                         '_channel_connected', False)),
                       'connected_since': getattr(self.agent, '_connected_since', None)},
            'task': {'id': task['id'], 'task_type': task['task_type'],
                     'project': task.get('project'), 'payload': task['payload']},
            'registration': {
                'project': task.get('project'), 'repo_cwd': row.get('repo_cwd'),
                'repo_exists': Path(row.get('repo_cwd') or '').is_dir(),
                'entry_doc_exists': Path(row.get('entry_doc') or '').is_file(),
                'rules_doc_exists': Path(row.get('rules_doc') or '').is_file(),
                'coordinator_agent': row.get('coordinator_agent')
                                     or cfg.get('coordinator_agent'),
                'coordinator_model': row.get('coordinator_model')
                                     or cfg.get('coordinator_model')},
            'watchdog': self._read_watchdog(task.get('project')),
            'control_plane': {'status': 'unknown', 'active_discover_executions': [],
                              'recent_discover_executions': 'unknown',
                              'items': 'unknown'},
            'worker_log': {'status': 'unknown',
                           'path': str(cp_runtime.data_home() / 'logs' /
                                       'worker-agent.jsonl')},
            'scheduler': {'discover': {'status': 'unknown'}},
            'missing_evidence': []}
        try:
            evidence['control_plane']['status'] = worker.status()
        except Exception as exc:
            evidence['missing_evidence'].append('control_plane.status: %s'
                                                % type(exc).__name__)
        try:
            evidence['control_plane']['active_discover_executions'] = \
                worker.executions(loop='discover')
        except Exception as exc:
            evidence['missing_evidence'].append('control_plane.executions: %s'
                                                % type(exc).__name__)
        try:
            evidence['control_plane']['recent_discover_executions'] = \
                worker.recent_executions(loop='discover', limit=20)
        except Exception as exc:
            evidence['missing_evidence'].append('control_plane.recent_executions: %s'
                                                % type(exc).__name__)
        evidence['task_freshness'] = self._diagnosis_freshness(
            task, evidence['control_plane'].get('recent_discover_executions'), now)
        if not Path(evidence['worker_log']['path']).is_file():
            evidence['missing_evidence'].append('worker_log: not collected by this install')
        try:
            items = worker.items(kind='discover')
            evidence['control_plane']['items'] = [
                {key: item.get(key) for key in
                 ('id', 'key', 'kind', 'state', 'created_at', 'version')}
                for item in items[:100] if isinstance(item, dict)]
        except Exception as exc:
            evidence['missing_evidence'].append('control_plane.items: %s'
                                                % type(exc).__name__)
        task_registration = (cfg.get('maintenance') or {}).get(
            'scheduler_tasks', {}).get('discover')
        if task_registration:
            try:
                state = self.maintenance.query_scheduler(task_registration)
                evidence['scheduler']['discover'] = {
                    'status': 'observed', 'registration': task_registration,
                    'state': state, 'observed_at': self.clock()}
            except Exception as exc:
                evidence['scheduler']['discover'] = {
                    'status': 'unknown', 'registration': task_registration,
                    'error': '%s: %s' % (type(exc).__name__, exc)}
                evidence['missing_evidence'].append('scheduler.discover: %s'
                                                    % type(exc).__name__)
        else:
            evidence['missing_evidence'].append('scheduler.discover: not registered')
        return evidence

    def _diagnose_project(self, task):
        project, row, cfg, agent_name, _model, argv = self._context(task)
        worker = self.agent.worker_factory(cfg)
        evidence = self._collect_evidence(task, row, cfg, worker)
        allowed = set(task.get('allowed_actions') or ()) & set(MAINTENANCE_ACTIONS)
        prompt = (
            'Analyze only the supplied timestamped evidence for this worker task. '
            'Do not execute tools or assume missing facts. Return one JSON object: '
            '{"summary": string, "findings": [{"claim": string, "evidence": string}], '
            '"actions": [{"id": string}]}. Choose actions only from allowed_actions; '
            'never invent arguments, shell, paths, or targets. The worker will check '
            'every requested action again before execution.\n\n'
            'allowed_actions=%s\nEVIDENCE_JSON=%s' % (
                json.dumps(sorted(allowed)),
                json.dumps(evidence, ensure_ascii=False, sort_keys=True)))
        stdout, log_path = self._run_session(task, agent_name, argv, prompt, row['repo_cwd'])
        text = self._output_text(stdout)
        if not text:
            raise ValueError('DIAGNOSIS_OUTPUT_EMPTY')
        answer = _extract_json_object(text)
        if answer is None:
            answer = {'summary': text[-4000:], 'findings': [], 'actions': []}
        summary = answer.get('summary') if isinstance(answer.get('summary'), str) else text
        findings = answer.get('findings') if isinstance(answer.get('findings'), list) else []
        requested = answer.get('actions') if isinstance(answer.get('actions'), list) else []
        actions, rejected = self._run_requested_actions(
            task, row, cfg, worker, evidence, requested, allowed)
        return {'summary': summary[:4000], 'findings': _bounded_findings(findings),
                'evidence': evidence, 'actions': actions,
                'rejected_actions': rejected, 'log_path': log_path,
                'session_id': (self._session_ids(stdout, text) or [None])[0]}

    def _active_discover(self, worker):
        executions = worker.executions(loop='discover')
        return executions

    def _diagnosis_freshness(self, task, recent, now=None):
        payload = task.get('payload') or {}
        expected = payload.get('last_execution_id')
        threshold = payload.get('threshold_seconds')
        now = self.clock() if now is None else now
        if not isinstance(expected, str) or not expected:
            return {'actionable': False, 'reason': 'stall event has no execution identity'}
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) \
                or threshold <= 0:
            return {'actionable': False, 'reason': 'stall event has no valid threshold'}
        if not isinstance(recent, list) or not recent:
            return {'actionable': False, 'reason': 'CP has no recent discover execution'}
        latest = recent[0]
        if not isinstance(latest, dict) or latest.get('id') != expected:
            return {'actionable': False, 'reason': 'discover execution advanced',
                    'latest_execution_id': latest.get('id') if isinstance(latest, dict) else None}
        created = latest.get('created_at')
        if isinstance(created, bool) or not isinstance(created, (int, float)):
            return {'actionable': False, 'reason': 'latest execution timestamp is unknown'}
        age = max(0.0, now - created)
        if age <= threshold:
            return {'actionable': False, 'reason': 'discover is within stall threshold',
                    'age_seconds': age, 'threshold_seconds': threshold}
        return {'actionable': True, 'reason': 'same discover execution remains stale',
                'latest_execution_id': expected, 'age_seconds': age,
                'threshold_seconds': threshold}

    def _run_requested_actions(self, task, row, cfg, worker, evidence,
                               requested, allowed):
        actions, rejected = [], []
        seen = set()
        for request in requested[:10]:
            action_id = request.get('id') if isinstance(request, dict) else request
            if not isinstance(action_id, str) or action_id not in allowed:
                rejected.append(str(action_id)[:200])
                continue
            if action_id in seen:
                actions.append({'id': action_id, 'state': 'DUPLICATE_ACTION',
                                'recorded_at': self.clock()})
                continue
            seen.add(action_id)
            try:
                actions.append(self._run_action(action_id, task, row, cfg,
                                                worker, evidence))
            except Exception as exc:
                actions.append({'id': action_id, 'state': 'FAILED',
                                'error': '%s: %s' % (type(exc).__name__, exc),
                                'recorded_at': self.clock()})
        return actions, rejected

    def _run_action(self, action_id, task, row, cfg, worker, evidence):
        record = {'id': action_id, 'project': task.get('project'),
                  'task_id': task['id'], 'recorded_at': self.clock()}
        freshness = self._diagnosis_freshness(
            task, worker.recent_executions(loop='discover', limit=20))
        record['task_freshness'] = freshness
        if not freshness.get('actionable'):
            record.update(state='STALE_SIGNAL', reason=freshness.get('reason'))
            return record
        if action_id == 'discover.run_once':
            active = self._active_discover(worker)
            record['precheck'] = {'active_discover_executions': active}
            if active:
                record.update(state='REFUSED_ACTIVE_EXECUTION', after=active)
                return record
            prompt = trigger.build_round_prompt('discover', row)
            try:
                command = json.loads(row['spawn_command']) \
                    if isinstance(row.get('spawn_command'), str) else row['spawn_command']
                argv = resolve_command(command)
                done = self.agent.agent_run_fn(
                    list(argv) + [prompt], cwd=row['repo_cwd'], capture_output=True,
                    text=True, timeout=RUNTIME_TIMEOUT_SECONDS, env=None)
            except Exception as exc:
                record.update(state='FAILED', error='%s: %s'
                              % (type(exc).__name__, exc))
                return record
            record['after'] = {'returncode': done.returncode,
                               'stdout_tail': (done.stdout or '')[-1000:],
                               'stderr_tail': (done.stderr or '')[-1000:]}
            record['state'] = 'SUCCEEDED' if done.returncode == 0 else 'FAILED'
            return record
        maintenance = cfg.get('maintenance') or {}
        task_registration = maintenance.get('scheduler_tasks', {}).get('discover')
        service_registration = maintenance.get('services', {}).get('worker_agent')
        if action_id in ('scheduler.start_registered',
                         'scheduler.restart_registered'):
            if not task_registration:
                raise ValueError('SCHEDULER_TASK_NOT_REGISTERED: discover')
            active = self._active_discover(worker)
            before = self.maintenance.query_scheduler(task_registration)
            record.update(precheck={'active_discover_executions': active,
                                    'task_enabled': before.get('enabled')},
                          before=before)
            if active:
                record.update(state='REFUSED_ACTIVE_EXECUTION', after=active)
                return record
            if not before.get('exists') or not before.get('enabled'):
                raise ValueError('SCHEDULER_TASK_NOT_READY')
            if action_id == 'scheduler.start_registered':
                if before.get('state') == 4 or before.get('state') == 'running':
                    record.update(state='NOOP_ALREADY_RUNNING', after=before)
                    return record
                issued = self.maintenance.start_scheduler(task_registration)
            else:
                issued = self.maintenance.restart_scheduler(task_registration)
            after = self.maintenance.query_scheduler(task_registration)
            record.update(issued=issued, after=after,
                          verified=(after.get('state') == 4
                                    or after.get('state') == 'running'),
                          state='SUCCEEDED' if (after.get('state') == 4
                                                or after.get('state') == 'running')
                          else 'POSTCHECK_FAILED')
            return record
        if action_id == 'service.start_if_missing':
            if not service_registration:
                raise ValueError('SERVICE_NOT_REGISTERED: worker_agent')
            active = worker.executions()
            before = self.maintenance.query_service(service_registration)
            record.update(precheck={'active_executions': active}, before=before)
            if active:
                record.update(state='REFUSED_ACTIVE_EXECUTION', after=active)
                return record
            if not before.get('exists'):
                raise ValueError('SERVICE_NOT_FOUND')
            if str(before.get('state', '')).lower() in ('running', '4'):
                record.update(state='NOOP_ALREADY_RUNNING', after=before)
                return record
            issued = self.maintenance.start_service(service_registration)
            after = self.maintenance.query_service(service_registration)
            verified = str(after.get('state', '')).lower() in ('running', '4')
            record.update(issued=issued, after=after, verified=verified,
                          state='SUCCEEDED' if verified else 'POSTCHECK_FAILED')
            return record
        raise ValueError('MAINTENANCE_ACTION_UNSUPPORTED: ' + action_id)


def _extract_json_object(text):
    text = text or ''
    candidates = [text]
    start = text.find('{')
    end = text.rfind('}')
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for raw in candidates:
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _bounded_value(value, depth=0):
    if isinstance(value, str):
        return value[:1000]
    if isinstance(value, dict):
        if depth >= 3:
            return '<nested value omitted>'
        return {str(key)[:100]: _bounded_value(item, depth + 1)
                for key, item in list(value.items())[:30]}
    if isinstance(value, list):
        if depth >= 3:
            return ['<nested values omitted>']
        return [_bounded_value(item, depth + 1) for item in value[:30]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


def _bounded_findings(findings):
    result = []
    for finding in findings[:30]:
        if isinstance(finding, dict):
            result.append({str(key)[:100]: _bounded_value(value)
                           for key, value in list(finding.items())[:10]})
        else:
            result.append(_bounded_value(finding))
    return result
