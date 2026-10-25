"""Machine-local execution journal for Operator worker tasks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import threading
import time


class WorkerTaskRunStore:
    LIVE = ('STARTING', 'RUNNING')

    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=30,
                                  isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA busy_timeout=30000')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS worker_task_runs (
                command_id TEXT PRIMARY KEY,
                task_type TEXT NOT NULL,
                project TEXT,
                state TEXT NOT NULL,
                session_id TEXT,
                execution_id TEXT,
                claim_token INTEGER,
                runtime_pid INTEGER,
                log_path TEXT,
                worktree TEXT,
                result_json TEXT,
                fail_reason TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
        ''')
        columns = {row[1] for row in self.db.execute(
            'PRAGMA table_info(worker_task_runs)')}
        if 'claim_token' not in columns:
            self.db.execute('ALTER TABLE worker_task_runs ADD COLUMN claim_token INTEGER')
        if 'runtime_pid' not in columns:
            self.db.execute('ALTER TABLE worker_task_runs ADD COLUMN runtime_pid INTEGER')
        if 'log_path' not in columns:
            self.db.execute('ALTER TABLE worker_task_runs ADD COLUMN log_path TEXT')
        if 'worktree' not in columns:
            self.db.execute('ALTER TABLE worker_task_runs ADD COLUMN worktree TEXT')
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._lock = threading.RLock()

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        result = dict(row)
        raw = result.pop('result_json')
        result['result'] = json.loads(raw) if raw is not None else None
        return result

    def close(self):
        self.db.close()

    def get(self, command_id):
        with self._lock:
            return self._decode(self.db.execute(
                'SELECT * FROM worker_task_runs WHERE command_id=?',
                (command_id,)).fetchone())

    def begin(self, command_id, task_type, project=None, now=None):
        if not isinstance(command_id, str) or not command_id:
            raise ValueError('WORKER_TASK_ID_INVALID')
        if not isinstance(task_type, str) or not task_type:
            raise ValueError('WORKER_TASK_TYPE_INVALID')
        now = time.time() if now is None else now
        with self._lock:
            row = self.db.execute(
                'SELECT * FROM worker_task_runs WHERE command_id=?',
                (command_id,)).fetchone()
            if row is not None:
                result = self._decode(row)
                if (result['task_type'], result['project']) != (task_type, project):
                    raise ValueError('WORKER_TASK_ID_CONFLICT')
                return result, False
            with self.db:
                self.db.execute(
                    "INSERT INTO worker_task_runs(command_id,task_type,project,state,"
                    'created_at,updated_at) VALUES (?,?,?,\'STARTING\',?,?)',
                    (command_id, task_type, project, now, now))
            return self.get(command_id), True

    def active_runs(self):
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM worker_task_runs WHERE state IN ('STARTING','RUNNING') "
                'ORDER BY created_at,command_id').fetchall()
            return [self._decode(row) for row in rows]

    def update(self, command_id, state, *, session_id=None, execution_id=None,
               claim_token=None, runtime_pid=None, log_path=None, result=None,
               worktree=None, fail_reason=None, now=None):
        state = (state or '').upper()
        if state not in ('RUNNING', 'SUCCEEDED', 'FAILED'):
            raise ValueError('WORKER_TASK_STATE_INVALID')
        if result is not None and not isinstance(result, dict):
            raise ValueError('WORKER_TASK_RESULT_INVALID')
        if runtime_pid is not None and (
                isinstance(runtime_pid, bool) or not isinstance(runtime_pid, int)
                or runtime_pid <= 0):
            raise ValueError('WORKER_TASK_PID_INVALID')
        if log_path is not None and (not isinstance(log_path, str) or not log_path):
            raise ValueError('WORKER_TASK_LOG_PATH_INVALID')
        if worktree is not None and (not isinstance(worktree, str) or not worktree):
            raise ValueError('WORKER_TASK_WORKTREE_INVALID')
        now = time.time() if now is None else now
        result_json = json.dumps(result, ensure_ascii=False, sort_keys=True) \
            if result is not None else None
        with self._lock:
            cur = self.db.execute(
                'UPDATE worker_task_runs SET state=?,session_id=coalesce(?,session_id),'
                'execution_id=coalesce(?,execution_id),'
                'claim_token=coalesce(?,claim_token),runtime_pid=coalesce(?,runtime_pid),'
                'log_path=coalesce(?,log_path),worktree=coalesce(?,worktree),'
                'result_json=coalesce(?,result_json),'
                'fail_reason=?,updated_at=? WHERE command_id=? AND state IN '
                "('STARTING','RUNNING')",
                (state, session_id, execution_id, claim_token, runtime_pid,
                 log_path, worktree, result_json,
                 fail_reason if state == 'FAILED' else None, now, command_id))
            if cur.rowcount != 1:
                return None
            return self.get(command_id)
