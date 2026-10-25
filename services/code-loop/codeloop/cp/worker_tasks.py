"""Durable machine-scoped tasks sent by the Operator to WorkerAgents.

Project round commands remain in each project's CP store. This ledger holds
typed tasks that target a machine (optionally scoped to a project), such as
worker diagnosis, and can also route project repair work to an authorized
worker without baking a model/runtime choice into the command.
"""
from __future__ import annotations

import json
import re
import secrets
import time


TASK_TYPES = ('pr.repair', 'worker.project_diagnose')
LIVE_STATES = ('PENDING', 'CLAIMED', 'RUNNING')
_PROJECT_KEY_RE = re.compile(r'^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+$')


class WorkerTaskLedger:
    SCHEMA = '''
        CREATE TABLE IF NOT EXISTS worker_tasks (
            id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL DEFAULT 1,
            task_type TEXT NOT NULL,
            target_machine_id TEXT,
            project TEXT,
            payload_json TEXT NOT NULL,
            allowed_actions_json TEXT NOT NULL DEFAULT '[]',
            source_event_id TEXT UNIQUE,
            dedupe_key TEXT,
            state TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            claimed_by TEXT,
            claimed_at REAL,
            attempts INTEGER NOT NULL DEFAULT 0,
            session_id TEXT,
            execution_id TEXT,
            result_json TEXT,
            fail_reason TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_worker_tasks_pending
            ON worker_tasks(state, target_machine_id, created_at, id);
        CREATE INDEX IF NOT EXISTS idx_worker_tasks_scope
            ON worker_tasks(task_type, project, dedupe_key, state);
        CREATE TABLE IF NOT EXISTS worker_task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES worker_tasks(id),
            machine_id TEXT NOT NULL,
            project TEXT,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_worker_task_events_machine
            ON worker_task_events(machine_id,id);
    '''

    def __init__(self, db):
        self.db = db
        self.db.executescript(self.SCHEMA)

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        result = dict(row)
        result['payload'] = json.loads(result.pop('payload_json'))
        result['allowed_actions'] = json.loads(
            result.pop('allowed_actions_json'))
        result['result'] = (json.loads(result.pop('result_json'))
                            if result.get('result_json') is not None else None)
        if 'result_json' in result:
            result.pop('result_json')
        return result

    def enqueue(self, task_type, *, project=None, target_machine_id=None,
                payload=None, allowed_actions=None, source_event_id=None,
                dedupe_key=None, schema_version=1, ttl=7 * 86400, now=None):
        """Insert a task, deduping both source replay and a live logical target.

        A project task with no target machine is routed to any currently
        connected machine granted for that project. A machine task is pinned
        to ``target_machine_id``. Project grant and machine state are checked
        again by the dispatcher immediately before delivery.
        """
        if task_type not in TASK_TYPES:
            raise ValueError('TASK_TYPE_UNKNOWN: %s' % task_type)
        if not isinstance(payload, dict):
            raise ValueError('TASK_PAYLOAD_INVALID: expected an object')
        if project is not None and (not isinstance(project, str)
                                    or not _PROJECT_KEY_RE.fullmatch(project)):
            raise ValueError('TASK_PROJECT_INVALID')
        if target_machine_id is not None and (
                not isinstance(target_machine_id, str) or not target_machine_id):
            raise ValueError('TASK_MACHINE_INVALID')
        if project is None and target_machine_id is None:
            raise ValueError('TASK_TARGET_REQUIRED')
        if not isinstance(schema_version, int) or isinstance(schema_version, bool) \
                or schema_version != 1:
            raise ValueError('TASK_SCHEMA_UNSUPPORTED')
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
            raise ValueError('TASK_TTL_INVALID')
        actions = [] if allowed_actions is None else allowed_actions
        if not isinstance(actions, (list, tuple)) or not all(
                isinstance(action, str) and action for action in actions):
            raise ValueError('TASK_ACTIONS_INVALID')
        actions = sorted(set(actions))
        if source_event_id is not None and (
                not isinstance(source_event_id, str) or not source_event_id):
            raise ValueError('TASK_SOURCE_INVALID')
        if dedupe_key is not None and (
                not isinstance(dedupe_key, str) or not dedupe_key):
            raise ValueError('TASK_DEDUPE_INVALID')
        now = time.time() if now is None else now
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        actions_json = json.dumps(actions, ensure_ascii=False, sort_keys=True)

        if source_event_id is not None:
            row = self.db.execute(
                'SELECT * FROM worker_tasks WHERE source_event_id=?',
                (source_event_id,)).fetchone()
            if row is not None:
                return self._decode(row), False
        if dedupe_key is not None:
            live = ','.join('?' for _ in LIVE_STATES)
            row = self.db.execute(
                'SELECT * FROM worker_tasks WHERE task_type=? '
                'AND coalesce(project,\'\')=coalesce(?,\'\') '
                'AND coalesce(target_machine_id,\'\')=coalesce(?,\'\') '
                'AND dedupe_key=? AND state IN (%s) '
                'ORDER BY created_at,id LIMIT 1' % live,
                (task_type, project, target_machine_id, dedupe_key,
                 *LIVE_STATES)).fetchone()
            if row is not None:
                return self._decode(row), False

        task_id = secrets.token_hex(16)
        with self.db:
            self.db.execute(
                'INSERT INTO worker_tasks '
                '(id,schema_version,task_type,target_machine_id,project,'
                'payload_json,allowed_actions_json,source_event_id,dedupe_key,'
                'state,created_at,updated_at,expires_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,\'PENDING\',?,?,?)',
                (task_id, schema_version, task_type, target_machine_id,
                 project, payload_json, actions_json, source_event_id,
                 dedupe_key, now, now, now + ttl))
        return self.get(task_id), True

    def pending_for_machine(self, machine_id, granted_projects=(), now=None):
        now = time.time() if now is None else now
        projects = sorted(set(granted_projects or ()))
        clauses = ['(target_machine_id=? AND project IS NULL)']
        params = [machine_id]
        if projects:
            marks = ','.join('?' for _ in projects)
            clauses.append('(target_machine_id=? AND project IN (%s))' % marks)
            params.extend([machine_id, *projects])
            clauses.append('(target_machine_id IS NULL AND project IN (%s))' % marks)
            params.extend(projects)
        sql = ('SELECT * FROM worker_tasks WHERE state=\'PENDING\' '
               'AND expires_at>? AND (%s) ORDER BY created_at,id'
               % ' OR '.join(clauses))
        return [self._decode(row) for row in self.db.execute(
            sql, (now, *params)).fetchall()]

    def requeue_expired_for_machine(self, machine_id, granted_projects=(), *,
                                    now=None, ttl=7 * 86400,
                                    schema_versions=None):
        """Revive expired offline tasks only when a granted worker reconnects.

        Task handlers revalidate PR heads and stall state before effects, so a
        long-offline task can be offered again without trusting stale input.
        """
        if not isinstance(machine_id, str) or not machine_id:
            raise ValueError('TASK_MACHINE_INVALID')
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
            raise ValueError('TASK_TTL_INVALID')
        versions = None
        if schema_versions is not None:
            versions = sorted({value for value in schema_versions
                               if isinstance(value, int)
                               and not isinstance(value, bool) and value > 0})
            if not versions:
                return 0
        now = time.time() if now is None else now
        projects = sorted(set(granted_projects or ()))
        clauses = ['(target_machine_id=? AND project IS NULL)']
        params = [machine_id]
        if projects:
            marks = ','.join('?' for _ in projects)
            clauses.append('(target_machine_id=? AND project IN (%s))' % marks)
            params.extend([machine_id, *projects])
            clauses.append('(target_machine_id IS NULL AND project IN (%s))' % marks)
            params.extend(projects)
        sql = ('SELECT id FROM worker_tasks WHERE state=\'EXPIRED\' AND (%s)'
               % ' OR '.join(clauses))
        if versions is not None:
            sql += ' AND schema_version IN (%s)' % ','.join('?' for _ in versions)
            params.extend(versions)
        rows = self.db.execute(sql, params).fetchall()
        changed = 0
        with self.db:
            for row in rows:
                cur = self.db.execute(
                    "UPDATE worker_tasks SET state='PENDING',expires_at=?,updated_at=?,"
                    "fail_reason='TASK_TTL_REQUEUED' WHERE id=? AND state='EXPIRED'",
                    (now + ttl, now, row['id']))
                changed += cur.rowcount
        return changed

    def mark_dispatched(self, task_id, machine_id, now=None):
        now = time.time() if now is None else now
        cur = self.db.execute(
            "UPDATE worker_tasks SET state='CLAIMED',claimed_by=?,claimed_at=?,"
            'updated_at=?,attempts=attempts+1 WHERE id=? AND state=\'PENDING\' '
            'AND expires_at>? AND (target_machine_id IS NULL OR target_machine_id=?)',
            (machine_id, now, now, task_id, now, machine_id))
        if cur.rowcount != 1:
            return None
        return self.get(task_id)

    def revert_pending(self, task_id, machine_id, reason=None, now=None):
        now = time.time() if now is None else now
        cur = self.db.execute(
            "UPDATE worker_tasks SET state='PENDING',claimed_by=NULL,claimed_at=NULL,"
            'updated_at=?,fail_reason=? WHERE id=? AND state=\'CLAIMED\' '
            'AND claimed_by=?',
            (now, reason, task_id, machine_id))
        if cur.rowcount != 1:
            return None
        return self.get(task_id)

    def report(self, task_id, machine_id, state, *, session_id=None,
               execution_id=None, result=None, fail_reason=None, now=None):
        """Record one authenticated worker state/result transition.

        A terminal result is immutable. This makes duplicate delivery and late
        process reports harmless; the machine identity is the task lease.
        """
        state = (state or '').upper()
        if state not in ('RUNNING', 'SUCCEEDED', 'FAILED'):
            raise ValueError('TASK_STATE_INVALID')
        if result is not None and not isinstance(result, dict):
            raise ValueError('TASK_RESULT_INVALID')
        now = time.time() if now is None else now
        result_json = (json.dumps(result, ensure_ascii=False, sort_keys=True)
                       if result is not None else None)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            previous = self.db.execute(
                'SELECT state FROM worker_tasks WHERE id=? AND claimed_by=?',
                (task_id, machine_id)).fetchone()
            cur = self.db.execute(
                'UPDATE worker_tasks SET state=?,updated_at=?,session_id=coalesce(?,session_id),'
                'execution_id=coalesce(?,execution_id),result_json=coalesce(?,result_json),'
                'fail_reason=? '
                'WHERE id=? AND claimed_by=? AND state IN (\'CLAIMED\',\'RUNNING\')',
                (state, now, session_id, execution_id, result_json,
                 fail_reason if state == 'FAILED' else None, task_id, machine_id))
            if cur.rowcount != 1:
                self.db.rollback()
                return None
            task = self.get(task_id)
            task_event_id = None
            if previous is not None and previous['state'] != state:
                task_event_id = self._append_task_event(task, state, machine_id, now)
            self.db.commit()
            task['task_event_id'] = task_event_id
            return task
        except Exception:
            self.db.rollback()
            raise

    def _append_task_event(self, task, state, machine_id, now):
        payload = {'task': task['id'], 'state': state,
                   'task_type': task['task_type'], 'project': task.get('project'),
                   'session_id': task.get('session_id'),
                   'execution_id': task.get('execution_id')}
        if state in ('SUCCEEDED', 'FAILED'):
            payload['result'] = task.get('result')
            payload['fail_reason'] = task.get('fail_reason')
        cur = self.db.execute(
            'INSERT INTO worker_task_events(task_id,machine_id,project,kind,'
            'payload_json,created_at) VALUES (?,?,?,?,?,?)',
            (task['id'], machine_id, task.get('project'),
             'worker.task.' + state.lower(),
             json.dumps(payload, ensure_ascii=False, sort_keys=True), now))
        event_id = cur.lastrowid
        payload['event_id'] = event_id
        self.db.execute('UPDATE worker_task_events SET payload_json=? WHERE id=?',
                        (json.dumps(payload, ensure_ascii=False, sort_keys=True), event_id))
        return event_id

    def events_after(self, machine_id, after=0, limit=100):
        if not isinstance(machine_id, str) or not machine_id:
            raise ValueError('TASK_MACHINE_INVALID')
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError('TASK_EVENT_CURSOR_INVALID')
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 1000:
            raise ValueError('TASK_LIMIT_INVALID')
        rows = self.db.execute(
            'SELECT id,task_id,machine_id,project,kind,payload_json,created_at '
            'FROM worker_task_events WHERE machine_id=? AND id>? '
            'ORDER BY id LIMIT ?', (machine_id, after, limit)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item['payload'] = json.loads(item.pop('payload_json'))
            result.append(item)
        return result

    def get(self, task_id):
        return self._decode(self.db.execute(
            'SELECT * FROM worker_tasks WHERE id=?', (task_id,)).fetchone())

    def list_tasks(self, *, state=None, target_machine_id=None, limit=100):
        if not isinstance(limit, int) or limit < 1 or limit > 1000:
            raise ValueError('TASK_LIMIT_INVALID')
        where = []
        params = []
        if state is not None:
            where.append('state=?')
            params.append(state)
        if target_machine_id is not None:
            where.append('(target_machine_id=? OR claimed_by=?)')
            params.extend((target_machine_id, target_machine_id))
        sql = 'SELECT * FROM worker_tasks'
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY created_at DESC,id DESC LIMIT ?'
        params.append(limit)
        return [self._decode(row) for row in self.db.execute(sql, params)]

    def sweep(self, now=None, ttl=1800, ack_window=600, max_attempts=3,
              running_timeout=5 * 60 * 60):
        now = time.time() if now is None else now
        counts = {'expired': 0, 'requeued': 0, 'failed': 0}
        self.db.execute('BEGIN IMMEDIATE')
        try:
            cur = self.db.execute(
                "UPDATE worker_tasks SET state='EXPIRED',fail_reason='TASK_TTL',"
                'updated_at=? WHERE state=\'PENDING\' AND expires_at<=?',
                (now, now))
            counts['expired'] = cur.rowcount
            stale = self.db.execute(
                "SELECT id,attempts,claimed_by FROM worker_tasks WHERE state='CLAIMED' "
                'AND claimed_at<=?', (now - ack_window,)).fetchall()
            for row in stale:
                if row['attempts'] >= max_attempts:
                    self.db.execute(
                        "UPDATE worker_tasks SET state='FAILED',"
                        "fail_reason='TASK_UNACKED',updated_at=? WHERE id=?",
                        (now, row['id']))
                    counts['failed'] += 1
                    if row['claimed_by']:
                        self._append_task_event(self.get(row['id']), 'FAILED',
                                                row['claimed_by'], now)
                else:
                    self.db.execute(
                        "UPDATE worker_tasks SET state='PENDING',claimed_by=NULL,"
                        'claimed_at=NULL,updated_at=? WHERE id=?', (now, row['id']))
                    counts['requeued'] += 1
            stale_running = self.db.execute(
                "SELECT id,claimed_by FROM worker_tasks WHERE state='RUNNING' "
                'AND updated_at<=?', (now - running_timeout,)).fetchall()
            for row in stale_running:
                changed = self.db.execute(
                    "UPDATE worker_tasks SET state='FAILED',"
                    "fail_reason='TASK_RUNNING_TIMEOUT',updated_at=? "
                    "WHERE id=? AND state='RUNNING'", (now, row['id'])).rowcount
                if changed:
                    counts['failed'] += 1
                    if row['claimed_by']:
                        self._append_task_event(self.get(row['id']), 'FAILED',
                                                row['claimed_by'], now)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return counts
