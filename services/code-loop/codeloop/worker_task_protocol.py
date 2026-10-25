"""Strict validation for machine-scoped Operator task frames."""
from __future__ import annotations

import math
import time


TASK_TYPES = ('pr.repair', 'worker.project_diagnose')
MAINTENANCE_ACTIONS = frozenset((
    'discover.run_once', 'scheduler.start_registered',
    'scheduler.restart_registered', 'service.start_if_missing'))

_COMMAND_FIELDS = frozenset(('id', 'action', 'project', 'target_machine_id', 'task'))
_TASK_FIELDS = frozenset(('id', 'schema_version', 'task_type', 'project',
                          'target_machine_id', 'payload', 'allowed_actions',
                          'expires_at', 'source_event_id'))


def _project(value):
    return (value is None or
            (isinstance(value, str) and len(value) <= 255
             and all(part and part not in ('.', '..')
                     for part in value.split('__'))
             and value.count('__') == 1
             and all(c.isalnum() or c in '._-' for c in value)))


def validate_worker_task(command, machine_id, now=None,
                         local_actions=MAINTENANCE_ACTIONS):
    if not isinstance(command, dict) or set(command) != _COMMAND_FIELDS:
        raise ValueError('TASK_FRAME_INVALID')
    task_id = command.get('id')
    if not isinstance(task_id, str) or not task_id:
        raise ValueError('TASK_ID_INVALID')
    if command.get('action') != 'worker_task':
        raise ValueError('TASK_ACTION_INVALID')
    if not _project(command.get('project')):
        raise ValueError('TASK_PROJECT_INVALID')
    task = command.get('task')
    if not isinstance(task, dict) or set(task) != _TASK_FIELDS:
        raise ValueError('TASK_ENVELOPE_INVALID')
    if task.get('id') != task_id:
        raise ValueError('TASK_ID_MISMATCH')
    if (isinstance(task.get('schema_version'), bool)
            or task.get('schema_version') != 1):
        raise ValueError('TASK_SCHEMA_UNSUPPORTED')
    task_type = task.get('task_type')
    if task_type not in TASK_TYPES:
        raise ValueError('TASK_TYPE_UNKNOWN: %r' % task_type)
    if task.get('project') != command.get('project') or not _project(task.get('project')):
        raise ValueError('TASK_PROJECT_MISMATCH')
    if (command.get('target_machine_id') != machine_id
            or task.get('target_machine_id') != machine_id):
        raise ValueError('TASK_MACHINE_MISMATCH')
    expiry = task.get('expires_at')
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) \
            or not math.isfinite(expiry):
        raise ValueError('TASK_EXPIRY_INVALID')
    now = time.time() if now is None else now
    if expiry <= now:
        raise ValueError('TASK_EXPIRED')
    source = task.get('source_event_id')
    if not isinstance(source, str) or not source:
        raise ValueError('TASK_SOURCE_INVALID')
    payload = task.get('payload')
    if not isinstance(payload, dict):
        raise ValueError('TASK_PAYLOAD_INVALID')
    if task_type == 'pr.repair':
        allowed = {'number', 'head_sha', 'base_ref', 'base_sha', 'reasons',
                   'source_ids', 'source_event_id'}
        number = payload.get('number')
        reasons = payload.get('reasons')
        if (set(payload) - allowed or isinstance(number, bool)
                or not isinstance(number, int) or number <= 0
                or not isinstance(reasons, list) or not reasons
                or not all(r in ('changes_requested', 'merge_conflict')
                           for r in reasons)):
            raise ValueError('TASK_PAYLOAD_INVALID: pr.repair')
    else:
        allowed = {'loop', 'last_execution_id', 'age_seconds',
                   'threshold_seconds', 'source_event_id'}
        if set(payload) - allowed or payload.get('loop') != 'discover':
            raise ValueError('TASK_PAYLOAD_INVALID: worker.project_diagnose')
    actions = task.get('allowed_actions')
    if not isinstance(actions, list) or not all(
            isinstance(action, str) and action for action in actions):
        raise ValueError('TASK_ACTIONS_INVALID')
    if task_type == 'pr.repair' and actions:
        raise ValueError('TASK_ACTIONS_INVALID: PR repair cannot carry maintenance grants')
    local_actions = set(local_actions or ())
    unsupported = sorted(set(actions) - local_actions)
    if unsupported:
        raise ValueError('TASK_ACTION_UNSUPPORTED: ' + ','.join(unsupported))
    return task
