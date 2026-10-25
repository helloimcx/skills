"""Server-level machine registry: identity, hashed credentials, project grants.

This is coordination-adjacent but deliberately separate from any project
store: machines belong to the server, not to a project's coordination state.
Token plaintext exists only in the enrollment/rotation response — the database
keeps sha256 hashes, so a registry leak is not a credential leak.
"""
from __future__ import annotations

import hmac
import json
from pathlib import Path
import secrets
import sqlite3
import time

from .worker_tasks import WorkerTaskLedger


class RegistryError(Exception):
    """Operator-facing registry rejection (coded, e.g. ENROLL_CODE_USED)."""


def _hash(value):
    import hashlib
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class Registry:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA busy_timeout=30000')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS machines (
                id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                token_hash TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                revoked INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, last_seen REAL, last_addr TEXT,
                agent_version TEXT DEFAULT '');
            CREATE TABLE IF NOT EXISTS grants (
                machine_id TEXT NOT NULL REFERENCES machines(id),
                org_repo TEXT NOT NULL, granted_at REAL NOT NULL,
                PRIMARY KEY (machine_id, org_repo));
            CREATE TABLE IF NOT EXISTS enroll_codes (
                code_hash TEXT PRIMARY KEY, name TEXT NOT NULL,
                projects TEXT NOT NULL, expires_at REAL NOT NULL,
                used_at REAL, used_by TEXT);
            CREATE TABLE IF NOT EXISTS join_requests (
                id TEXT PRIMARY KEY, name TEXT NOT NULL,
                secret_hash TEXT NOT NULL, cert_fp TEXT NOT NULL DEFAULT '',
                agent_version TEXT DEFAULT '',
                state TEXT NOT NULL,
                projects TEXT NOT NULL DEFAULT '[]',
                approved_name TEXT,
                requested_at REAL NOT NULL, decided_at REAL,
                expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS grant_requests (
                id TEXT PRIMARY KEY, machine_id TEXT NOT NULL REFERENCES machines(id),
                org_repo TEXT NOT NULL,
                state TEXT NOT NULL,
                requested_at REAL NOT NULL, decided_at REAL);
            CREATE INDEX IF NOT EXISTS idx_grant_requests_pending
                ON grant_requests(state, requested_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_grant_requests_one_pending
                ON grant_requests(machine_id, org_repo) WHERE state='PENDING';
        ''')
        self.worker_tasks = WorkerTaskLedger(self.db)

    def close(self):
        self.db.close()

    def create_enroll_code(self, name, projects, ttl=600):
        if not isinstance(name, str) or not name.strip():
            raise RegistryError('MACHINE_NAME_REQUIRED')
        if self.db.execute('SELECT 1 FROM machines WHERE name=?', (name,)).fetchone():
            raise RegistryError('MACHINE_NAME_TAKEN: revoke the old machine first')
        pending = self.db.execute(
            'SELECT 1 FROM enroll_codes WHERE name=? AND used_at IS NULL AND expires_at>?',
            (name, time.time())).fetchone()
        if pending:
            raise RegistryError('MACHINE_NAME_TAKEN: an unused enroll code already exists')
        if not isinstance(projects, list) or not all(isinstance(p, str) and p for p in projects):
            raise RegistryError('INVALID_ENROLL_PROJECTS')
        code = secrets.token_urlsafe(24)
        self.db.execute('INSERT INTO enroll_codes(code_hash,name,projects,expires_at) VALUES (?,?,?,?)',
                        (_hash(code), name, json.dumps(sorted(set(projects))), time.time() + ttl))
        return code

    def enroll(self, code, agent_version, addr):
        row = self.db.execute('SELECT * FROM enroll_codes WHERE code_hash=?', (_hash(code),)).fetchone()
        if row is None:
            raise RegistryError('ENROLL_CODE_INVALID')
        if row['used_at'] is not None:
            raise RegistryError('ENROLL_CODE_USED: one-time codes cannot enroll twice')
        if row['expires_at'] <= time.time():
            raise RegistryError('ENROLL_CODE_EXPIRED')
        machine_id = secrets.token_hex(16)
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.db:
            self.db.execute('''INSERT INTO machines(id,name,token_hash,enabled,revoked,created_at,agent_version)
                VALUES (?,?,?,1,0,?,?)''', (machine_id, row['name'], _hash(token), now, agent_version or ''))
            for project in json.loads(row['projects']):
                self.db.execute('INSERT INTO grants(machine_id,org_repo,granted_at) VALUES (?,?,?)',
                                (machine_id, project, now))
            self.db.execute('UPDATE enroll_codes SET used_at=?,used_by=? WHERE code_hash=?',
                            (now, machine_id, row['code_hash']))
        return {'machine_id': machine_id, 'name': row['name'], 'token': token,
                'projects': json.loads(row['projects'])}

    def authenticate(self, token):
        if not isinstance(token, str) or not token:
            return None
        digest = _hash(token)
        for row in self.db.execute('SELECT * FROM machines').fetchall():
            # Constant-time compare across the (small) registry: a leaked
            # timing channel must not allow hash recovery.
            if hmac.compare_digest(row['token_hash'], digest):
                return dict(row)
        return None

    def machine_grants(self, machine_id):
        """The project keys one machine is granted for — the truth a WS
        hello's self-announced registrations are filtered against."""
        return {row['org_repo'] for row in self.db.execute(
            'SELECT org_repo FROM grants WHERE machine_id=?', (machine_id,))}

    # ---------------------------------------------------- join / approve pairing
    # Worker-initiated enrollment: the worker posts a pairing request (secret
    # hash + the server cert fingerprint it observed), an operator approves on
    # the server — where the reported fingerprint is auto-compared against the
    # server's own certificate — and the worker picks the token up with the
    # pairing secret plaintext, which until then never leaves the worker.

    def _expire_joins(self, now):
        with self.db:
            self.db.execute("UPDATE join_requests SET state='EXPIRED' "
                            "WHERE state IN ('PENDING','APPROVED') AND expires_at<=?",
                            (now,))

    def _join_row(self, name):
        # The latest request for this name: terminal rows may be followed by a
        # fresh PENDING one (re-join after REJECT/EXPIRED), so never match on
        # name alone without ordering.
        return self.db.execute('SELECT * FROM join_requests WHERE name=? '
                               'ORDER BY requested_at DESC, rowid DESC LIMIT 1',
                               (name,)).fetchone()

    def create_join(self, name, secret_hash, cert_fp, agent_version='', now=None, ttl=900):
        if not isinstance(name, str) or not name.strip():
            raise RegistryError('MACHINE_NAME_REQUIRED')
        name = name.strip()
        now = time.time() if now is None else now
        if self.db.execute('SELECT 1 FROM machines WHERE name=?', (name,)).fetchone():
            raise RegistryError('JOIN_NAME_TAKEN: a machine with this name already exists')
        self._expire_joins(now)
        # Only an active (PENDING/APPROVED) request blocks a re-join; terminal
        # rows (REJECTED/EXPIRED) are history and the name may be reused.
        active = self.db.execute(
            "SELECT 1 FROM join_requests WHERE name=? AND state IN ('PENDING','APPROVED')",
            (name,)).fetchone()
        if active is not None:
            raise RegistryError('JOIN_NAME_PENDING: a join request with this name '
                                'is already waiting for approval')
        join_id = secrets.token_hex(16)
        with self.db:
            self.db.execute('''INSERT INTO join_requests
                (id,name,secret_hash,cert_fp,agent_version,state,requested_at,expires_at)
                VALUES (?,?,?,?,?,'PENDING',?,?)''',
                            (join_id, name, secret_hash, cert_fp or '',
                             agent_version or '', now, now + ttl))
        return dict(self.db.execute('SELECT * FROM join_requests WHERE id=?',
                                    (join_id,)).fetchone())

    def list_joins(self, now=None):
        self._expire_joins(time.time() if now is None else now)
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM join_requests WHERE state IN ('PENDING','APPROVED') "
            'ORDER BY requested_at')]

    def approve_join(self, name, projects, server_cert_fp=None, as_name=None,
                     now=None, ttl=900):
        # 零项目合法：注册（身份）与授权（grants）解耦，授权可事后独立追加
        if not isinstance(projects, list) or \
                not all(isinstance(p, str) and p for p in projects):
            raise RegistryError('INVALID_JOIN_PROJECTS')
        now = time.time() if now is None else now
        self._expire_joins(now)
        row = self._join_row(name)
        if row is None:
            raise RegistryError('JOIN_NOT_FOUND: no join request named ' + name)
        if row['state'] == 'EXPIRED':
            raise RegistryError('JOIN_EXPIRED: request timed out, let the worker re-join')
        if row['state'] != 'PENDING':
            raise RegistryError('JOIN_NOT_PENDING: request state is ' + row['state'])
        if server_cert_fp is not None and row['cert_fp'] != server_cert_fp:
            raise RegistryError(
                'JOIN_FINGERPRINT_MISMATCH: worker reported certificate %s but this '
                "server's certificate is %s — a man-in-the-middle is likely; reject "
                'this request' % (row['cert_fp'] or '(none)', server_cert_fp))
        approved_name = (as_name or name).strip()
        if self.db.execute('SELECT 1 FROM machines WHERE name=?', (approved_name,)).fetchone():
            raise RegistryError('JOIN_NAME_TAKEN: a machine named %s already exists'
                                % approved_name)
        with self.db:
            self.db.execute('''UPDATE join_requests SET state='APPROVED', approved_name=?,
                projects=?, decided_at=?, expires_at=? WHERE id=?''',
                            (approved_name, json.dumps(sorted(set(projects))),
                             now, now + ttl, row['id']))
        return dict(self._join_row(name))

    def reject_join(self, name, now=None):
        now = time.time() if now is None else now
        self._expire_joins(now)
        row = self._join_row(name)
        if row is None:
            raise RegistryError('JOIN_NOT_FOUND: no join request named ' + name)
        if row['state'] != 'PENDING':
            raise RegistryError('JOIN_NOT_PENDING: request state is ' + row['state'])
        with self.db:
            self.db.execute("UPDATE join_requests SET state='REJECTED', decided_at=? "
                            'WHERE id=?', (now, row['id']))

    def consume_join(self, name, secret, now=None):
        """Exchange the pairing secret for a machine token (one time)."""
        now = time.time() if now is None else now
        self._expire_joins(now)
        row = self._join_row(name)
        if row is None:
            raise RegistryError('JOIN_NOT_FOUND: no join request named ' + name)
        if row['state'] == 'PENDING':
            raise RegistryError('JOIN_NOT_APPROVED: waiting for operator approval')
        if row['state'] == 'REJECTED':
            raise RegistryError('JOIN_REJECTED: the operator declined this request')
        if row['state'] == 'EXPIRED':
            raise RegistryError('JOIN_EXPIRED: request timed out, let the worker re-join')
        if row['state'] == 'CONSUMED':
            raise RegistryError('JOIN_ALREADY_CONSUMED: the token was already picked up')
        digest = _hash(secret) if isinstance(secret, str) else ''
        if not hmac.compare_digest(row['secret_hash'], digest):
            raise RegistryError('JOIN_SECRET_MISMATCH: pairing secret does not match')
        machine_name = row['approved_name'] or row['name']
        if self.db.execute('SELECT 1 FROM machines WHERE name=?', (machine_name,)).fetchone():
            raise RegistryError('JOIN_NAME_TAKEN: a machine named %s already exists'
                                % machine_name)
        machine_id = secrets.token_hex(16)
        token = secrets.token_urlsafe(32)
        projects = json.loads(row['projects'])
        with self.db:
            self.db.execute('''INSERT INTO machines(id,name,token_hash,enabled,revoked,created_at,agent_version)
                VALUES (?,?,?,1,0,?,?)''', (machine_id, machine_name, _hash(token),
                                            now, row['agent_version'] or ''))
            for project in projects:
                self.db.execute('INSERT INTO grants(machine_id,org_repo,granted_at) VALUES (?,?,?)',
                                (machine_id, project, now))
            self.db.execute("UPDATE join_requests SET state='CONSUMED', decided_at=? WHERE id=?",
                            (now, row['id']))
        return {'machine_id': machine_id, 'name': machine_name, 'token': token,
                'projects': projects}

    def consume_join_state(self, name, now=None):
        """Read a join request's current state without consuming it — the
        worker's poll path while it waits for operator approval."""
        self._expire_joins(time.time() if now is None else now)
        row = self._join_row(name)
        if row is None:
            raise RegistryError('JOIN_NOT_FOUND: no join request named ' + name)
        return {'state': row['state'], 'name': row['name'],
                'expires_at': row['expires_at']}

    def touch(self, machine_id, addr, agent_version=None):
        if agent_version:
            self.db.execute('UPDATE machines SET last_seen=?,last_addr=?,agent_version=? WHERE id=?',
                            (time.time(), addr, agent_version, machine_id))
        else:
            self.db.execute('UPDATE machines SET last_seen=?,last_addr=? WHERE id=?',
                            (time.time(), addr, machine_id))

    def require_write_access(self, machine, org_repo):
        if machine['revoked']:
            raise RegistryError('MACHINE_REVOKED')
        if not machine['enabled']:
            raise RegistryError('MACHINE_DISABLED')
        if not self.has_grant(machine['id'], org_repo):
            raise RegistryError('PROJECT_NOT_GRANTED')

    # ------------------------------------------- worker-proposed grant requests
    # 提案可以来自 worker（凭 token 鉴权、服务端盖章归属），批准永远在服务端：
    # 这里只维护请求队列，写 grants 的入口仍只有 approve/grant 等操作者动作。

    def create_grant_request(self, machine_id, org_repo, now=None):
        if not org_repo or not isinstance(org_repo, str):
            raise RegistryError('REQUEST_PROJECT_REQUIRED')
        if self.db.execute('SELECT 1 FROM machines WHERE id=?', (machine_id,)).fetchone() is None:
            raise RegistryError('MACHINE_NOT_FOUND')
        if self.has_grant(machine_id, org_repo):
            return {'state': 'ALREADY_GRANTED', 'machine_id': machine_id,
                    'org_repo': org_repo}
        now = time.time() if now is None else now
        row = self.db.execute('''SELECT id FROM grant_requests
            WHERE machine_id=? AND org_repo=? AND state='PENDING' ''',
                               (machine_id, org_repo)).fetchone()
        if row is not None:
            return {'id': row['id'], 'state': 'PENDING', 'machine_id': machine_id,
                    'org_repo': org_repo}
        request_id = secrets.token_hex(16)
        try:
            with self.db:
                self.db.execute('''INSERT INTO grant_requests
                    (id,machine_id,org_repo,state,requested_at) VALUES (?,?,?,'PENDING',?)''',
                                (request_id, machine_id, org_repo, now))
        except sqlite3.IntegrityError:
            # 并发首发同 (machine, project)：唯一 PENDING 索引兜底，回读既有提案
            row = self.db.execute('''SELECT id FROM grant_requests
                WHERE machine_id=? AND org_repo=? AND state='PENDING' ''',
                                   (machine_id, org_repo)).fetchone()
            if row is not None:
                return {'id': row['id'], 'state': 'PENDING', 'machine_id': machine_id,
                        'org_repo': org_repo}
            raise
        return {'id': request_id, 'state': 'PENDING', 'machine_id': machine_id,
                'org_repo': org_repo}

    def list_grant_requests(self, state='PENDING'):
        """Queue view: pending proposals by default — the operator queue must
        not grow without bound (decided history stays in sqlite)."""
        sql = '''SELECT g.id, g.machine_id, m.name AS machine_name, g.org_repo,
                   g.state, g.requested_at, g.decided_at
            FROM grant_requests g JOIN machines m ON m.id = g.machine_id'''
        if state is None:
            rows = self.db.execute(sql + ' ORDER BY g.requested_at')
        else:
            rows = self.db.execute(sql + ' WHERE g.state=? ORDER BY g.requested_at',
                                   (state,))
        return [dict(r) for r in rows]

    def _grant_request_row(self, request_id):
        return self.db.execute('SELECT * FROM grant_requests WHERE id=?',
                               (request_id,)).fetchone()

    def approve_grant_request(self, request_id, now=None):
        row = self._grant_request_row(request_id)
        if row is None:
            raise RegistryError('REQUEST_NOT_FOUND: no grant request ' + str(request_id))
        if row['state'] != 'PENDING':
            raise RegistryError('REQUEST_NOT_PENDING: request state is ' + row['state'])
        now = time.time() if now is None else now
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO grants(machine_id,org_repo,granted_at) '
                            'VALUES (?,?,?)', (row['machine_id'], row['org_repo'], now))
            self.db.execute("UPDATE grant_requests SET state='APPROVED',decided_at=? WHERE id=?",
                            (now, request_id))
        return {'id': request_id, 'state': 'APPROVED',
                'machine_name': self.db.execute('SELECT name FROM machines WHERE id=?',
                                                (row['machine_id'],)).fetchone()['name'],
                'org_repo': row['org_repo']}

    def reject_grant_request(self, request_id, now=None):
        row = self._grant_request_row(request_id)
        if row is None:
            raise RegistryError('REQUEST_NOT_FOUND: no grant request ' + str(request_id))
        if row['state'] != 'PENDING':
            raise RegistryError('REQUEST_NOT_PENDING: request state is ' + row['state'])
        with self.db:
            self.db.execute("UPDATE grant_requests SET state='REJECTED',decided_at=? WHERE id=?",
                            (time.time() if now is None else now, request_id))

    def has_grant(self, machine_id, org_repo):
        return self.db.execute('SELECT 1 FROM grants WHERE machine_id=? AND org_repo=?',
                               (machine_id, org_repo)).fetchone() is not None

    def grant(self, name, org_repo):
        machine = self.db.execute('SELECT id FROM machines WHERE name=?', (name,)).fetchone()
        if machine is None:
            raise RegistryError('MACHINE_NOT_FOUND')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO grants(machine_id,org_repo,granted_at) VALUES (?,?,?)',
                            (machine['id'], org_repo, time.time()))

    def ungrant(self, name, org_repo):
        with self.db:
            self.db.execute('''DELETE FROM grants WHERE org_repo=? AND machine_id=
                (SELECT id FROM machines WHERE name=?)''', (org_repo, name))

    def set_enabled(self, name, enabled):
        with self.db:
            self.db.execute('UPDATE machines SET enabled=? WHERE name=?',
                            (1 if enabled else 0, name))

    def revoke(self, name):
        with self.db:
            self.db.execute('UPDATE machines SET revoked=1,enabled=0 WHERE name=?', (name,))

    def rotate(self, name):
        machine = self.db.execute('SELECT id FROM machines WHERE name=?', (name,)).fetchone()
        if machine is None:
            raise RegistryError('MACHINE_NOT_FOUND')
        token = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute('UPDATE machines SET token_hash=? WHERE id=?',
                            (_hash(token), machine['id']))
        return token

    def list_machines(self):
        rows = self.db.execute('''SELECT m.id, m.name, m.enabled, m.revoked, m.created_at,
            m.last_seen, m.last_addr, m.agent_version,
            (SELECT group_concat(org_repo, ',') FROM grants g WHERE g.machine_id=m.id) AS projects
            FROM machines m ORDER BY m.created_at, m.name''').fetchall()
        return [dict(r) for r in rows]

    def machine_projects(self, machine_id):
        return [r['org_repo'] for r in self.db.execute(
            'SELECT org_repo FROM grants WHERE machine_id=? ORDER BY org_repo', (machine_id,))]
