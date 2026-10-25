"""Single-host SQLite coordination with derived terminal states.

Terminal states are derived from delivered facts (a complete report with a
clean outbox), never declared by the worker. The fencing guard checks token
and state only — the clock never closes a write channel; a lease's expiry is
merely the reclaim trigger: an expired execution keeps its resources until
another claim steals them (which is itself the fence — the steal terminates
the victim in the same transaction), so an unstealable-but-slow worker can
always finish. Unresolved publications (SENDING/UNKNOWN) are fenced by the
red line instead: they are never auto-terminalized and keep their resources
until an operator reconciles a verified remote receipt.
"""
from contextlib import contextmanager, nullcontext
import json
import math
from pathlib import Path
import sqlite3
import sys
import time
from urllib.parse import quote
import uuid

from .command_store import ACTIVE, CommandStore
from .runtime import version


TERMINAL = ('SUCCEEDED', 'FAILED', 'BLOCKED', 'CANCELLED')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _resources(values):
    if not isinstance(values, list) or not values:
        raise ValueError('resources must be a non-empty list (use path:* for unknown scope)')
    result = []
    for value in values:
        if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
            raise ValueError('invalid resource')
        if value.startswith('path:'):
            scope = value[5:].rstrip('/')
            if not scope or scope.startswith('/') or any(x in ('', '.', '..') for x in scope.split('/')):
                raise ValueError('path resource must be a normalized repository-relative scope')
            if '*' in scope and scope != '*':
                raise ValueError('only path:* wildcard is supported')
            value = 'path:' + scope
        elif not value.startswith(('issue-', 'pr-', 'branch-', 'gate-', 'op:')):
            raise ValueError('resource must be issue-, pr-, branch-, gate-, op:, or path:')
        if value not in result:
            result.append(value)
    return sorted(result)


def _conflicts(left, right):
    if set(left).intersection(right):
        return True
    # Path resources only exclude other path work: '*' covers every scope,
    # concrete scopes conflict on path prefix overlap. Object resources
    # (issue-/pr-/branch-) denote platform-object ownership and never
    # conflict with a path scope, or handler rounds would wedge every claim.
    a_paths = [x[5:] for x in left if x.startswith('path:')]
    b_paths = [x[5:] for x in right if x.startswith('path:')]
    return any(a == '*' or b == '*' or a.startswith(b + '/') or b.startswith(a + '/')
               for a in a_paths for b in b_paths)


class Store(CommandStore):
    def __init__(self, state_dir, check_same_thread=True):
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir = self.state_dir / 'locks'
        self.locks_dir.mkdir(exist_ok=True)
        self.db = sqlite3.connect(str(self.state_dir / 'control-plane.sqlite'),
                                  timeout=30, isolation_level=None,
                                  check_same_thread=check_same_thread)
        self.db.row_factory = sqlite3.Row
        # Optional event sink (operator subscription, 2026-09-30): invoked
        # after every event() insert. Must never touch this store (callers
        # hold the write transaction) and never break the write path.
        self.on_event = None
        try:
            self._init_schema()
        except Exception:
            # A corrupt store must not keep its file handle (the dashboard's
            # trigger opens stores per click and reports the failure loudly).
            self.db.close()
            raise

    def _init_schema(self):
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA busy_timeout=30000')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS work_items (
                id TEXT PRIMARY KEY, key TEXT NOT NULL, kind TEXT NOT NULL,
                version TEXT NOT NULL, payload TEXT NOT NULL, resources TEXT NOT NULL,
                state TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, UNIQUE(key, version)
            );
            CREATE TABLE IF NOT EXISTS executions (
                id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES work_items(id),
                worker TEXT NOT NULL, token INTEGER NOT NULL UNIQUE,
                expires_at REAL NOT NULL, state TEXT NOT NULL,
                worktree TEXT, branch TEXT, head TEXT, result TEXT,
                item_version TEXT NOT NULL, created_at REAL NOT NULL,
                last_seen_at REAL
            );
            CREATE TABLE IF NOT EXISTS leases (
                resource TEXT PRIMARY KEY, execution_id TEXT NOT NULL REFERENCES executions(id),
                token INTEGER NOT NULL, expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evidence (
                id TEXT PRIMARY KEY, execution_id TEXT NOT NULL REFERENCES executions(id),
                kind TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                payload TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, execution_id TEXT NOT NULL REFERENCES executions(id),
                action TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
                receipt TEXT
            );
            CREATE TABLE IF NOT EXISTS commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL, number INTEGER NOT NULL,
                state TEXT NOT NULL, target_machine TEXT,
                claimed_by TEXT, claimed_at REAL, spawned_at REAL,
                attempts INTEGER NOT NULL DEFAULT 0, fail_reason TEXT,
                execution TEXT, agent TEXT, model TEXT,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_commands_state_created
                ON commands(state, created_at);
            CREATE INDEX IF NOT EXISTS idx_executions_state_expires
                ON executions(state, expires_at);
            CREATE INDEX IF NOT EXISTS idx_outbox_execution_id
                ON outbox(execution_id, state);
            CREATE INDEX IF NOT EXISTS idx_executions_item_created
                ON executions(item_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_events_kind_created
                ON events(kind, created_at);
            CREATE TABLE IF NOT EXISTS progress_events (
                execution TEXT NOT NULL, seq INTEGER NOT NULL,
                payload TEXT NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY (execution, seq)
            );
        ''')
        # Lightweight migration for stores created before the execution column
        # (this branch's own intermediate shape; production stores never had it).
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(commands)')}
        if columns and 'execution' not in columns:
            self.db.execute('ALTER TABLE commands ADD COLUMN execution TEXT')
        # Same migration shape for the spawn-option columns (agent/model): a
        # nullable pair, NULL unless the click chose a non-default agent.
        if columns and 'agent' not in columns:
            self.db.execute('ALTER TABLE commands ADD COLUMN agent TEXT')
        if columns and 'model' not in columns:
            self.db.execute('ALTER TABLE commands ADD COLUMN model TEXT')
        # Last protocol-activity timestamp of a round: the orphan-collapse
        # liveness signal (guard refreshes it). Legacy rows carry NULL and
        # fall back to created_at — except rounds that already bound a
        # worktree/branch: those are demonstrably alive (a corpse never
        # reaches bind), so the migration timestamps them now to keep the
        # first threshold computation from collapsing healthy long rounds
        # (review MAJOR-3: COALESCE fallback would read their claim-time
        # created_at as 2h+ silence).
        exec_columns = {row[1] for row in self.db.execute('PRAGMA table_info(executions)')}
        if exec_columns and 'last_seen_at' not in exec_columns:
            self.db.execute('ALTER TABLE executions ADD COLUMN last_seen_at REAL')
            self.db.execute(
                "UPDATE executions SET last_seen_at=? "
                "WHERE last_seen_at IS NULL AND (worktree IS NOT NULL OR branch IS NOT NULL)",
                (time.time(),))

    # ------------------------------ session progress (agent-reported stream)
    # The events themselves are opaque to the control plane: the agent parses
    # its machine-local session output and reports flat plugin events verbatim.
    # The store only owns the contract that makes the stream trustworthy —
    # idempotent by (execution, seq) so retries never duplicate — and the
    # retention that clears it with the command ledger's own repair pass.

    def append_progress(self, execution, events, now=None):
        """Store one batch of reported events; returns how many rows were
        actually added (re-reported seqs are ignored, not an error). Seqs are
        1-based, so the reader's initial cursor 0 misses nothing."""
        now = time.time() if now is None else now
        if not isinstance(execution, str) or not execution:
            raise ValueError('PROGRESS_EXECUTION: execution must be a non-empty string')
        clean = []
        for event in events or []:
            seq = (event or {}).get('seq')
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
                raise ValueError('PROGRESS_SEQ: seq must be a positive integer')
            clean.append((execution, seq, _json(event), now))
        with self.transaction():
            added = 0
            for row in clean:
                cur = self.db.execute(
                    'INSERT OR IGNORE INTO progress_events(execution, seq, payload, '
                    'created_at) VALUES (?,?,?,?)', row)
                added += cur.rowcount
            return added

    def progress_rows(self, execution, after_seq=0):
        """The stored stream for one execution, seq ascending, strictly past
        ``after_seq`` — the reader's incremental cursor."""
        return [dict(seq=row['seq'], payload=json.loads(row['payload']),
                     created_at=row['created_at'])
                for row in self.db.execute(
                    'SELECT seq, payload, created_at FROM progress_events '
                    'WHERE execution=? AND seq>? ORDER BY seq',
                    (execution, after_seq))]

    @contextmanager
    def transaction(self):
        if self.db.in_transaction:
            raise ValueError('nested transactions are not supported')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield self.db
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def event(self, kind, payload):
        self.db.execute('INSERT INTO events(kind,payload,created_at) VALUES (?,?,?)',
                        (kind, _json(payload), time.time()))
        if self.on_event is not None:
            try:
                self.on_event(kind, payload)
            except Exception as exc:  # loud, never fatal to the writer
                print('codeloop: event sink failed for %s: %s' % (kind, exc),
                      file=sys.stderr)

    def put_item(self, key, kind, version, payload, resources, priority=0, state='READY'):
        if not all(isinstance(x, str) and x for x in (key, kind, version)):
            raise ValueError('key, kind and version must be non-empty strings')
        if state not in ('READY',) + TERMINAL:
            raise ValueError('items cannot be inserted as active executions')
        resource_json = _json(_resources(resources))
        payload_json = _json(payload)
        with (nullcontext() if self.db.in_transaction else self.transaction()):
            old = self.db.execute('SELECT * FROM work_items WHERE key=? AND version=?', (key, version)).fetchone()
            if old is not None:
                if old['state'] == 'CANCELLED':
                    # The world re-derived this exact version: the dead round
                    # never delivered an effect, so the key is claimable again
                    # (a SUCCEEDED verdict is a fact and stays).
                    self.db.execute("UPDATE work_items SET state='READY' WHERE id=?", (old['id'],))
                    self.event('item.revived', {'id': old['id'], 'key': key, 'version': version})
                    return dict(old, state='READY')
                return dict(old)
            self.db.execute("UPDATE work_items SET state='CANCELLED' WHERE key=? AND state='READY'", (key,))
            item_id = uuid.uuid4().hex
            self.db.execute('''INSERT INTO work_items (id,key,kind,version,payload,resources,state,priority,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                (item_id, key, kind, version, payload_json, resource_json, state, priority, time.time()))
            self.event('item.created', {'id': item_id, 'key': key, 'version': version})
            return dict(self.db.execute('SELECT * FROM work_items WHERE id=?', (item_id,)).fetchone())

    def items(self, kind=None):
        if kind is None:
            rows = self.db.execute('SELECT * FROM work_items ORDER BY created_at,id')
        else:
            rows = self.db.execute('SELECT * FROM work_items WHERE kind=? ORDER BY created_at,id', (kind,))
        return [dict(row) for row in rows]

    def blocked_snapshot(self, kind='handler', limit=3, window=None):
        """同因 BLOCKED 连续判定的有界快照（issue #280 评审 MAJOR-2）。

        只在 handler 角色项、SUCCEEDED 且有 result 的终结执行中数连续段，
        时间窗（默认 30 天）内每 **key**（跨所有 item 版本）最多取最近
        ``limit`` 行——连续计数只需尾部即可判定。窗口函数替代全表扫 + 全量
        JSON 解析：查询成本与活跃工作项成正比、与历史总量无关，数据量 100×
        时 sync 仍不退化；执行按 (item_id, created_at) 复合索引扫描。

        返回 {key: [execution-row, ...]}（时间升序）；行含 state/result 与
        排序键 created_at，供 blocked.consecutive 直接消费。
        """
        if window is None:
            window = 30 * 86400
        cutoff = time.time() - window
        rows = self.db.execute('''
            SELECT key, state, result, created_at FROM (
              SELECT w.key AS key, e.state AS state, e.result AS result,
                     e.created_at AS created_at, e.id AS id,
                     ROW_NUMBER() OVER (PARTITION BY w.key
                       ORDER BY e.created_at DESC, e.id DESC) AS rn
                FROM executions e JOIN work_items w ON w.id = e.item_id
               WHERE w.kind = ? AND e.state = 'SUCCEEDED'
                 AND e.result IS NOT NULL AND e.created_at >= ?
            ) WHERE rn <= ? ORDER BY created_at, id
        ''', (kind, cutoff, limit)).fetchall()
        out = {}
        for row in rows:
            out.setdefault(row['key'], []).append({'state': row['state'],
                                                   'result': row['result'],
                                                   'created_at': row['created_at']})
        return out

    @staticmethod
    def _ttl(ttl):
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or ttl <= 0:
            raise ValueError('ttl must be a finite positive number')
        return ttl

    def lock_path(self, resource):
        # issue/pr names remain readable to legacy loops. Slash in branches must
        # be encoded, never treated as a path or removed by replacing characters.
        return self.locks_dir / quote(resource, safe='-_.')

    def _lock_owner_is_dead(self, path):
        """A leftover lock is reclaimable only when its recorded owner is a
        terminal execution of this store (terminal state commits before the
        file is released, so this is a crash leftover). Unknown, unreadable,
        or still-active owners are preserved: never delete what we cannot
        prove we own."""
        try:
            owner = json.loads((path / 'agent.json').read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError):
            return False
        row = self.db.execute('SELECT state FROM executions WHERE id=?',
                              (owner.get('execution_id'),)).fetchone()
        return row is not None and row['state'] in TERMINAL

    def _acquire_lock_file(self, resource, owner):
        """Create the lock file, taking over a provably dead leftover."""
        path = self.lock_path(resource)
        try:
            path.mkdir()
        except FileExistsError:
            if not self._lock_owner_is_dead(path):
                return None
            # Takeover rewrites the owner in place; unexpected extra files
            # still indicate interference and are preserved untouched.
            if set(x.name for x in path.iterdir()) - {'agent.json'}:
                return None
        (path / 'agent.json').write_text(_json(owner), encoding='utf-8')
        return path

    def _release_locks(self, resources, execution_id, token):
        for resource in reversed(resources):
            if resource.startswith('path:'):
                continue
            path = self.lock_path(resource)
            try:
                owner_file = path / 'agent.json'
                owner = json.loads(owner_file.read_text(encoding='utf-8'))
                if owner.get('execution_id') != execution_id or owner.get('token') != token:
                    continue
                # Unexpected files indicate interference; preserve rather than
                # recursively deleting any content belonging to another worker.
                if set(x.name for x in path.iterdir()) != {'agent.json'}:
                    continue
                owner_file.unlink()
                path.rmdir()
            except (OSError, ValueError, TypeError):
                # Missing/changed/unreadable locks are conservative leftovers.
                continue

    def claim(self, kind, worker, ttl=1800, limit=3, item_id=None):
        self._ttl(ttl)
        if not isinstance(worker, str) or not worker or isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError('worker and positive integer limit are required')
        now = time.time()
        created_locks = []
        stolen = []
        stolen_ids = set()
        execution_id, token = None, None
        result = None
        # Directed claims (item_id given) that lose to a resource conflict
        # report who holds the blocking resources (issue #254) — the generic
        # "unavailable" wording hid the real blocker and its unlock time.
        self.last_blocked = None
        try:
            with self.transaction():
                active = self.db.execute('''SELECT e.*, w.resources, w.kind, w.key AS item_key
                    FROM executions e JOIN work_items w ON w.id=e.item_id
                    WHERE e.state IN ('RUNNING','VERIFYING')''').fetchall()
                # Parallel slots count live leases only: expired executions are
                # reclaim targets, not capacity. A claim lost to full slots
                # names the capacity holders (who, until when) — a bare None
                # read as "coordinator broken" masked a zombie-saturated pool
                # (ai_vector, 2026-09-30).
                blocked = self._capacity_blocked(active, kind, limit, now)
                if blocked is not None:
                    self.last_blocked = blocked
                    return None
                query = "SELECT * FROM work_items WHERE kind=? AND state='READY'"
                args = [kind]
                if item_id is not None:
                    query += ' AND (id=? OR key=?)'
                    args += [item_id, item_id]
                query += ' ORDER BY priority DESC,created_at,id'
                for item in self.db.execute(query, args).fetchall():
                    resources = json.loads(item['resources'])
                    blockers = [row for row in active
                                if row['id'] not in stolen_ids
                                and _conflicts(resources, json.loads(row['resources']))]
                    # Expired blockers with a clean outbox are stolen (the steal
                    # is the fence: the victim is terminalized in this same
                    # transaction). Live or limbo (unresolved publication)
                    # blockers keep their resources; the candidate skips.
                    victims = [row for row in blockers
                               if row['expires_at'] <= now
                               and not self.db.execute(
                                   "SELECT 1 FROM outbox WHERE execution_id=? AND state!='SENT'",
                                   (row['id'],)).fetchone()]
                    if any(row not in victims for row in blockers):
                        # A directed claim reports its blockers (issue #254):
                        # the caller turns this into an honest "held by X until
                        # Y" refusal instead of a generic unavailability. Only
                        # the first skipped candidate is captured — the queue
                        # is ordered by priority, so that is the one the user
                        # asked about.
                        self._record_blocked(item_id, blockers, victims)
                        continue
                    seq = self.db.execute("SELECT value FROM meta WHERE key='fencing_token'").fetchone()
                    token = int(seq['value']) + 1 if seq else 1
                    execution_id = uuid.uuid4().hex
                    expires_at = now + ttl
                    for victim in victims:
                        stolen_ids.add(victim['id'])
                        stolen.append((victim['id'], victim['token'],
                                       self._cancel_victim_locked(victim, execution_id, worker)))
                    owner = {'execution_id': execution_id, 'token': token, 'agent_id': worker,
                             'worker': worker, 'acquired_at': now, 'expires_at': expires_at}
                    acquired = True
                    for resource in resources:
                        if resource.startswith('path:'):
                            continue
                        if self._acquire_lock_file(resource, owner) is None:
                            acquired = False
                            break
                        created_locks.append(resource)
                    if not acquired:
                        self._release_locks(created_locks, execution_id, token)
                        created_locks = []
                        continue
                    self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES ('fencing_token',?)", (str(token),))
                    self.db.execute('''INSERT INTO executions
                        (id,item_id,worker,token,expires_at,state,item_version,created_at,last_seen_at)
                        VALUES (?,?,?,?,?,'RUNNING',?,?,?)''',
                        (execution_id, item['id'], worker, token, expires_at,
                         item['version'], now, now))
                    for resource in resources:
                        self.db.execute('INSERT INTO leases VALUES (?,?,?,?)',
                                        (resource, execution_id, token, expires_at))
                    self.db.execute("UPDATE work_items SET state='RUNNING' WHERE id=?", (item['id'],))
                    self.event('execution.claimed', {'id': execution_id, 'item_id': item['id'], 'token': token})
                    result = dict(self.db.execute('SELECT * FROM executions WHERE id=?', (execution_id,)).fetchone())
                    # The report payload column is definitionally null at claim
                    # time and must not leak into the response: sessions read
                    # 「result: null」 off the object as the empty-round marker
                    # (ai_quality_analysis dropped 14 claimed reviews that way,
                    # 2026-10-08/09). Every carrier sits on this row.
                    result.pop('result', None)
                    break
            # Terminal states commit before any lock file moves: a crash here
            # leaves blocking locks, never a live execution without its files.
            # Both the claimed and the empty outcome pass through here, so
            # stolen victims are released even when no candidate wins.
            for victim_id, victim_token, victim_resources in stolen:
                self._release_locks(victim_resources, victim_id, victim_token)
            return result
        except BaseException:
            # Only remove complete ownership records from this attempted claim.
            # A crash during mkdir/owner write intentionally leaves an orphan;
            # stolen victims stay untouched because their terminalization
            # rolls back with the transaction.
            if execution_id is not None:
                self._release_locks(created_locks, execution_id, token)
            raise

    @staticmethod
    def _capacity_blocked(active, kind, limit, now):
        """Holder snapshot when the kind's parallel slots are full, else None.
        Expired leases are not capacity and never appear; live leases are not
        stealable, so the snapshot only tells the caller who until when."""
        holders = [row for row in active
                   if row['kind'] == kind and row['expires_at'] > now]
        if len(holders) < limit:
            return None
        return [{'item_key': row['item_key'], 'kind': row['kind'],
                 'worker': row['worker'], 'state': row['state'],
                 'resources': json.loads(row['resources']),
                 'expires_at': row['expires_at'], 'stealable': False}
                for row in holders]

    def _record_blocked(self, item_id, blockers, victims):
        """Snapshot the resource-conflict holders for a directed claim that
        lost (issue #254): item key, worker, state, conflicting resources,
        expiry, and whether the holder is stealable. The caller turns this
        into an honest refusal instead of a generic unavailability. Only the
        first skipped candidate is captured — the queue is ordered by
        priority, so that is the one the user asked about."""
        if item_id is None or self.last_blocked is not None:
            return
        self.last_blocked = [
            {'item_key': b['item_key'], 'kind': b['kind'],
             'worker': b['worker'], 'state': b['state'],
             'resources': json.loads(b['resources']),
             'expires_at': b['expires_at'],
             'stealable': b in victims}
            for b in blockers]

    def _cancel_victim_locked(self, victim, stolen_by, worker):
        """Transaction-scoped steal of an expired, outbox-clean execution."""
        self.db.execute("UPDATE executions SET state='CANCELLED' WHERE id=?", (victim['id'],))
        self._requeue_or_cancel_item(victim['item_id'])
        self.db.execute('DELETE FROM leases WHERE execution_id=?', (victim['id'],))
        self.event('execution.stolen', {'id': victim['id'], 'stolen_by': stolen_by, 'worker': worker})
        return json.loads(self.db.execute('SELECT resources FROM work_items WHERE id=?',
                                          (victim['item_id'],)).fetchone()['resources'])

    def _requeue_or_cancel_item(self, item_id):
        """A dead round without effects re-runs: the item goes back to READY
        unless sync already produced a newer READY successor for the key."""
        item = self.db.execute('SELECT key FROM work_items WHERE id=?', (item_id,)).fetchone()
        successor = self.db.execute(
            "SELECT 1 FROM work_items WHERE key=? AND state='READY' AND id!=?",
            (item['key'], item_id)).fetchone()
        state = 'CANCELLED' if successor else 'READY'
        self.db.execute('UPDATE work_items SET state=? WHERE id=?', (state, item_id))

    def guard(self, execution, token):
        if isinstance(token, bool) or not isinstance(token, int):
            raise ValueError('fencing token must be an integer')
        row = self.db.execute('SELECT * FROM executions WHERE id=?', (execution,)).fetchone()
        if row is None or row['token'] != token or row['state'] not in ACTIVE:
            raise ValueError('execution is missing, fenced, or terminal')
        # Every protocol call passes here: the refresh is the round's liveness
        # signal for orphan collapse, not a lease assertion (renew owns that).
        self.db.execute('UPDATE executions SET last_seen_at=? WHERE id=?',
                        (time.time(), execution))
        return dict(row)

    def renew(self, execution, token, ttl=1800):
        """Occupancy renewal (formerly heartbeat): an assertion against steal,
        not a survival requirement — guard no longer checks the clock."""
        self._ttl(ttl)
        with self.transaction():
            self.guard(execution, token)
            self._renew_locked(execution, ttl)
            return self.guard(execution, token)

    def _renew_locked(self, execution, ttl):
        expires_at = time.time() + ttl
        self.db.execute('UPDATE executions SET expires_at=? WHERE id=?', (expires_at, execution))
        self.db.execute('UPDATE leases SET expires_at=? WHERE execution_id=?', (expires_at, execution))

    def finalize_locked(self, execution):
        """Transaction-scoped terminal derivation (D1-D3). Terminal states are
        derived from facts, never declared: a delivered report whose outbox is
        fully SENT (or which requested no publication) proves the round
        delivered its obligations. Returns the resources whose lock files the
        caller must release after commit, or None when the facts do not yet
        prove completion. The report's decision carries the business outcome;
        SUCCEEDED means the protocol obligations were met."""
        if not self.db.in_transaction:
            raise ValueError('finalize_locked requires an open transaction')
        row = self.db.execute('SELECT * FROM executions WHERE id=?', (execution,)).fetchone()
        if row is None or row['state'] not in ACTIVE or not row['result']:
            return None
        if self.db.execute("SELECT 1 FROM outbox WHERE execution_id=? AND state!='SENT'",
                           (execution,)).fetchone():
            return None
        try:
            result = json.loads(row['result'])
            if not isinstance(result, dict):
                raise ValueError('not a mapping')
            if result.get('actions') is not None:
                actions = result['actions']
                if not isinstance(actions, list):
                    return None
                # An empty list is the normal no-publication report shape
                # used by operator rounds. Non-empty actions are the Discover
                # batch protocol and require every child outbox to be SENT.
                for index, action in enumerate(actions):
                    if (not isinstance(action, dict)
                            or action.get('action') != 'create_issue'):
                        return None
                    operation = version([execution, 'report-action', index], {})
                    if not self.db.execute(
                            "SELECT 1 FROM outbox WHERE id=? AND state='SENT'",
                            (operation,)).fetchone():
                        return None
            elif result.get('action') and not self.db.execute(
                    "SELECT 1 FROM outbox WHERE id=? AND state='SENT'",
                    (version([execution, result], {}),)).fetchone():
                return None
        except (ValueError, TypeError):
            # A corrupt result cannot be proven complete; the sweep's
            # conservative cancel path handles it instead.
            return None
        resources = json.loads(self.db.execute('SELECT resources FROM work_items WHERE id=?',
                                               (row['item_id'],)).fetchone()['resources'])
        self.db.execute("UPDATE executions SET state='SUCCEEDED' WHERE id=?", (execution,))
        self.db.execute("UPDATE work_items SET state='SUCCEEDED' WHERE id=?", (row['item_id'],))
        self.db.execute('DELETE FROM leases WHERE execution_id=?', (execution,))
        self.event('execution.derived', {'id': execution, 'state': 'SUCCEEDED'})
        return resources

    def clean_source(self, value):
        """Strip our own operation markers from platform facts.

        Only ignore our own known operation markers, not arbitrary external
        text. Reviewer comments are protocol receipts and must not become new
        review input. Issue comments remain source evidence: discover may
        deliberately append a risk finding to an existing Issue.
        """
        markers = ['<!-- code-loop:' + r['id'] + ' -->' for r in self.db.execute(
            "SELECT id FROM outbox WHERE state='SENT' AND action IN ('review_comment','comment_pr')")]

        def own_comment(v):
            # Issue/PR entities also contain body and can legitimately carry
            # our create marker. Only filter comment-shaped objects; deleting
            # an entity would erase its relation and freeze an old input version.
            return (isinstance(v, dict) and 'body' in v and 'number' not in v
                    and 'title' not in v and 'head' not in v
                    and any(m in (v.get('body') or '') for m in markers))

        def clean(v):
            if isinstance(v, list):
                return [clean(x) for x in v if not own_comment(x)]
            if isinstance(v, dict):
                return {k: clean(x) for k, x in v.items()
                        if k not in ('updated_at', 'comments_count', 'review_comments', 'comments_url')}
            return v

        return clean(value)

    def cancel_stale(self, e, item):
        """D6: a round built on superseded input is cancelled by the
        coordinator, not by an agent ritual. Only safe with a clean outbox —
        an unresolved publication keeps the execution and its resources until
        an operator reconciles it."""
        with self.transaction():
            self.guard(e['id'], e['token'])
            if self.db.execute(
                    "SELECT 1 FROM outbox WHERE execution_id=? AND state!='SENT'",
                    (e['id'],)).fetchone():
                raise ValueError('STALE_INPUT: unresolved publication; reconcile first')
            self.db.execute("UPDATE executions SET state='CANCELLED' WHERE id=?", (e['id'],))
            self.db.execute("UPDATE work_items SET state='CANCELLED' WHERE id=?", (item['id'],))
            self.db.execute('DELETE FROM leases WHERE execution_id=?', (e['id'],))
            self.event('execution.stale_cancelled', {'id': e['id'], 'key': item['key']})
            resources = json.loads(item['resources'])
        self._release_locks(resources, e['id'], e['token'])

    def derive_terminal(self, execution):
        """Standalone derivation for the sweep, the reconcile tail, and tests."""
        with self.transaction():
            resources = self.finalize_locked(execution)
            row = self.db.execute('SELECT * FROM executions WHERE id=?', (execution,)).fetchone()
        if resources is not None:
            self._release_locks(resources, row['id'], row['token'])
        return dict(row) if resources is not None else None

    def abandon(self, execution, token):
        """Holder-side abandonment: the round never started (or stops before any
        effect), so its lease ends now and the standard sweep derivation closes
        it — CANCELLED + item back to READY + locks released — instead of after
        the full lease. Refused once a publication was attempted: a possibly
        successful external write is never auto-declared failed (operator
        reconcile territory)."""
        with self.transaction():
            self.guard(execution, token)
            if self.db.execute("SELECT 1 FROM outbox WHERE execution_id=? AND state!='SENT'",
                               (execution,)).fetchone():
                raise ValueError('UNFINISHED_PUBLICATION: reconcile before abandoning')
            self.db.execute('UPDATE executions SET expires_at=? WHERE id=?',
                            (time.time() - 1, execution))
            self.event('execution.abandoned', {'id': execution})
        self._sweep_one(execution)
        return execution

    def sweep_expired(self):
        """Reclaim expired executions (claim-time bookkeeping). Clean outbox +
        delivered report -> derived SUCCEEDED (D4); clean outbox without a
        usable report -> CANCELLED with the item re-READY (D5): no publication
        was attempted, so a re-run needs no operator. Unresolved publications
        stay ACTIVE for the operator — a possibly-successful external write is
        never auto-declared failed. Returns the swept execution ids."""
        swept = []
        rows = self.db.execute('''SELECT id FROM executions
            WHERE state IN ('RUNNING','VERIFYING') AND expires_at<=?''',
            (time.time(),)).fetchall()
        for row in rows:
            if self._sweep_one(row['id']):
                swept.append(row['id'])
        return swept

    def collapse_idle(self, idle_seconds):
        """Reclaim silent, publication-clean rounds before their lease lapses
        (claim-time bookkeeping, runs beside sweep_expired). A round that died
        right after claim otherwise holds its slot for the whole lease; the
        liveness signal is last_seen_at (every protocol call refreshes it via
        guard), falling back to created_at for legacy rows. Limbo is spared:
        an execution with an unresolved publication is operator territory no
        matter how silent. idle_seconds <= 0 disables. Returns the collapsed
        execution ids.

        Accepted race: a guard landing in the milliseconds between the shrink
        commit and _sweep_one does not rescue the round — a 2h-silent round
        whose next protocol call finally lands loses it to a fence (terminal
        state) and the item is re-READY for a fresh attempt; by contract the
        silence itself already made the round reclaimable."""
        now = time.time()
        if not idle_seconds or idle_seconds <= 0:
            return []
        threshold = now - idle_seconds
        # Index-friendly window (idx_executions_state_expires +
        # idx_outbox_execution_id): state/expiry bound the scan, and the
        # LIMIT caps any single claim's cleanup work — 100x rows change the
        # cost, not the shape (review MAJOR-2).
        with self.transaction():
            rows = self.db.execute('''SELECT id FROM executions
                WHERE state IN ('RUNNING','VERIFYING') AND expires_at>?
                  AND COALESCE(last_seen_at, created_at)<=?
                  AND NOT EXISTS (SELECT 1 FROM outbox
                                  WHERE execution_id=executions.id AND state!='SENT')
                LIMIT 1000''',
                (now, threshold)).fetchall()
            for row in rows:
                self.db.execute('UPDATE executions SET expires_at=? WHERE id=?',
                                (now, row['id']))
        swept = []
        for row in rows:
            if self._sweep_one(row['id']):
                swept.append(row['id'])
        return swept

    def _sweep_one(self, execution):
        with self.transaction():
            row = self.db.execute('SELECT * FROM executions WHERE id=?', (execution,)).fetchone()
            if row is None or row['state'] not in ACTIVE or row['expires_at'] > time.time():
                return None
            if self.db.execute("SELECT 1 FROM outbox WHERE execution_id=? AND state!='SENT'",
                               (execution,)).fetchone():
                return None  # limbo: operator reconcile territory (red line)
            # D4 vs D5 share finalize_locked as the single completeness oracle:
            # derivable -> SUCCEEDED; otherwise a usable report is absent (or
            # its requested publication never happened) -> CANCELLED + re-run.
            resources = self.finalize_locked(execution)
            if resources is None:
                self.db.execute("UPDATE executions SET state='CANCELLED' WHERE id=?", (execution,))
                self._requeue_or_cancel_item(row['item_id'])
                self.db.execute('DELETE FROM leases WHERE execution_id=?', (execution,))
                self.event('execution.swept', {'id': execution, 'state': 'CANCELLED'})
                resources = json.loads(self.db.execute('SELECT resources FROM work_items WHERE id=?',
                                                       (row['item_id'],)).fetchone()['resources'])
        self._release_locks(resources, execution, row['token'])
        return execution

    def reconcile(self, operation, receipt):
        """Record an operator-confirmed remote receipt without retrying a write."""
        if not isinstance(receipt, dict) or not receipt:
            raise ValueError('REMOTE_RECEIPT_REQUIRED')
        with self.transaction():
            row = self.db.execute('SELECT * FROM outbox WHERE id=?', (operation,)).fetchone()
            if row is None or row['state'] == 'SENT':
                raise ValueError('OUTBOX_NOT_RECONCILABLE')
            self.db.execute("UPDATE outbox SET state='SENT',receipt=? WHERE id=?",
                            (_json(receipt), operation))
            self.event('publication.reconciled', {'operation': operation})
            resources = self.finalize_locked(row['execution_id'])
        if resources is not None:
            execution = self.db.execute('SELECT * FROM executions WHERE id=?',
                                        (row['execution_id'],)).fetchone()
            self._release_locks(resources, execution['id'], execution['token'])
        return dict(self.db.execute('SELECT * FROM outbox WHERE id=?', (operation,)).fetchone())

    def add_evidence(self, execution, token, kind, payload):
        with self.transaction():
            self.guard(execution, token)
            evidence_id = uuid.uuid4().hex
            self.db.execute('INSERT INTO evidence VALUES (?,?,?,?,?)',
                            (evidence_id, execution, kind, _json(payload), time.time()))
            self.event('evidence.added', {'id': evidence_id, 'execution_id': execution, 'kind': kind})
            return dict(self.db.execute('SELECT * FROM evidence WHERE id=?', (evidence_id,)).fetchone())

    def evidence(self, execution, kind=None):
        query = 'SELECT * FROM evidence WHERE execution_id=?'
        args = [execution]
        if kind is not None:
            query += ' AND kind=?'
            args.append(kind)
        return [dict(row) for row in self.db.execute(query + ' ORDER BY created_at,id', args)]

    def status(self):
        return {
            'items': {row['state']: row['n'] for row in self.db.execute(
                'SELECT state,COUNT(*) AS n FROM work_items GROUP BY state')},
            'executions': {row['state']: row['n'] for row in self.db.execute(
                'SELECT state,COUNT(*) AS n FROM executions GROUP BY state')},
            'expired_executions': self.db.execute(
                "SELECT COUNT(*) FROM executions WHERE state IN ('RUNNING','VERIFYING') AND expires_at<=?",
                (time.time(),)).fetchone()[0],
            'outbox': {row['state']: row['n'] for row in self.db.execute(
                'SELECT state,COUNT(*) AS n FROM outbox GROUP BY state')},
        }
