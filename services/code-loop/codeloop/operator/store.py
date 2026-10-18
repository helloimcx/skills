"""Operator machine-level store.

Signals are ordinary work items in a dedicated machine-global Store (the CP
Store class, own state dir under ~/.code-loop/operator/state) — kind
'operator', key 'op:<source>:<signal-key>'. Reuse buys claim/lease/progress/
events/commands semantics for free and makes operator rounds plain
executions, so the existing view=run component renders them unmodified.
"""
import json
import os
import time
import uuid
from pathlib import Path

from ..cp.store import Store


def default_home():
    """The machine-local data root: ``CODE_LOOP_HOME`` when set (test/CI
    isolation, same convention as cp.runtime.data_home), else ~/.code-loop."""
    return Path(os.environ.get('CODE_LOOP_HOME')
                or (Path.home() / '.code-loop')).expanduser()


def state_dir(home=None):
    return (Path(home) if home else default_home()) / 'operator' / 'state'


def open_store(home=None, check_same_thread=False):
    """Open (creating on first use) the machine-level operator store.

    The operator store is the one CP store shared across threads by design
    (the serve side: consumer thread + HTTP handler threads on one Store
    instance, review BLOCKER-2) — sqlite3 serializes access itself, so the
    connection is opened thread-shared. Project-level stores keep their
    single-thread default."""
    return Store(state_dir(home), check_same_thread=check_same_thread)


def log_path(home=None):
    return (Path(home) if home else default_home()) / 'logs' / 'operator-log.jsonl'


def signal_key(source, key):
    return 'op:%s:%s' % (source, key)


def active_signal(store, source, key):
    """The live READY item for a signal, or None (terminal items are history:
    the signal may fire a fresh round)."""
    row = store.db.execute(
        "SELECT * FROM work_items WHERE key=? AND state='READY' "
        'ORDER BY created_at DESC, id DESC LIMIT 1',
        (signal_key(source, key),)).fetchone()
    return dict(row) if row is not None else None


def put_signal(store, *, source, kind, key, payload, org_repo='',
               severity='', version=None, now=None):
    """Record one signal occurrence as an operator work item.

    Same (key, version) is idempotent; a re-fired signal carries a fresh
    version, superseding (cancelling) the previous READY item per put_item
    semantics. Returns (item_dict, created)."""
    now = time.time() if now is None else now
    version = version or '%.6f-%s' % (now, uuid.uuid4().hex[:8])
    full = dict(payload or {}, source=source, kind=kind, key=key,
                org_repo=org_repo or '', severity=severity or 'mid',
                received_at=now)
    item = store.put_item(key=signal_key(source, key), kind='operator',
                          version=version, payload=full,
                          resources=[signal_key(source, key)])
    created = item.get('version') == version
    return item, created


def item_for_number(store, number):
    """The work item behind an operator command's integer number (the
    work_items rowid taken at enqueue time)."""
    row = store.db.execute(
        "SELECT * FROM work_items WHERE rowid=? AND kind='operator'",
        (number,)).fetchone()
    return dict(row) if row is not None else None


def recycle_after_fail(store, execution, token, reason):
    """Cascade a FAILED operator verdict from its execution to the parent item
    (issue #302 defect 1).

    ``live`` verdicts are only ever reached by a round that claimed an item
    (the watchdog's PROCESS_DIED path — round.py ``_watch``) — the parent item
    was left RUNNING and neither ``active_signal`` (READY-only) nor the next
    ``put_signal`` release it, so the same signal key is wedged under a zombie
    RUNNING row holding its resource lease. Terminalize the item, drop its
    leases and lock file, so a re-fired signal (new version) can reclaim the
    key. BLOCKER-1 discipline is preserved: the execution UPDATE is a no-op on
    a state already terminal, and only a live round may be failed here."""
    with store.transaction():
        updated = store.db.execute(
            "UPDATE executions SET state='FAILED', result=? "
            "WHERE id=? AND token=? AND state IN ('RUNNING','VERIFYING')",
            (json.dumps({'outcome': None, 'error': reason}), execution, token)).rowcount
        if not updated:
            return None
        row = store.db.execute('SELECT item_id FROM executions WHERE id=?',
                               (execution,)).fetchone()
        resources = None
        if row is not None:
            item = store.db.execute('SELECT * FROM work_items WHERE id=?',
                                    (row['item_id'],)).fetchone()
            if item is not None and item['state'] in ('READY', 'RUNNING'):
                store.db.execute("UPDATE work_items SET state='CANCELLED' WHERE id=?",
                                 (item['id'],))
                resources = json.loads(item['resources'])
                store.db.execute('DELETE FROM leases WHERE execution_id=?', (execution,))
                store.event('operator.item_recycled', {'execution': execution,
                                                       'key': item['key'],
                                                       'reason': reason})
    if resources is not None:
        store._release_locks(resources, execution, token)
    return execution


def recycle_wedged_rounds(store, limit=50):
    """Floor hygiene (2026-10-15 复盘): a RUNNING operator item whose
    executions are all terminal is wedged forever — the pre-#302 PROCESS_DIED
    vintage left RUNNING items holding expired leases, and every re-fired
    same-key signal died at claim time on UNIQUE(leases.resource) (~90 FAILED
    commands over 7 days). ``recycle_after_fail`` only reaches live rounds and
    ``sweep_expired`` only scans non-terminal executions, so the wedge has no
    recovery path; this is it. Leases of terminal executions are leaks by
    definition — every finalizer releases them, a leftover is an orphan — so
    they are dropped in the same pass. Live rounds carry a non-terminal
    execution and are never touched. Bounded by ``limit`` (items) per pass;
    the serve consumer's ledger floor calls this every tick. Returns
    ``{'items': .., 'leases': ..}``."""
    wedged = store.db.execute(
        """SELECT id, key FROM work_items
           WHERE kind='operator' AND state='RUNNING'
             AND EXISTS (SELECT 1 FROM executions WHERE item_id=work_items.id)
             AND NOT EXISTS (SELECT 1 FROM executions
                             WHERE item_id=work_items.id
                               AND state IN ('RUNNING','VERIFYING'))
           ORDER BY created_at LIMIT ?""", (limit,)).fetchall()
    items = 0
    leases = 0
    freed = []
    for row in wedged:
        with store.transaction():
            updated = store.db.execute(
                "UPDATE work_items SET state='CANCELLED' "
                "WHERE id=? AND state='RUNNING'", (row['id'],)).rowcount
            if not updated:
                continue   # raced with another recycler — CAS guard, no event
            items += 1
            # Review F2: the recycled item's terminal-execution leases die in
            # the SAME transaction as the item — a phase gap here would let a
            # re-fired signal claim straight into the leftover lease and fail
            # on UNIQUE(leases.resource), the exact defect being healed.
            doomed = store.db.execute(
                """SELECT resource, execution_id, token FROM leases
                   WHERE execution_id IN
                   (SELECT id FROM executions WHERE item_id=?
                    AND state NOT IN ('RUNNING','VERIFYING'))""",
                (row['id'],)).fetchall()
            leases += store.db.execute(
                """DELETE FROM leases WHERE execution_id IN
                   (SELECT id FROM executions WHERE item_id=?
                    AND state NOT IN ('RUNNING','VERIFYING'))""",
                (row['id'],)).rowcount
            freed.extend(dict(r) for r in doomed)
            store.event('operator.item_recycled', {'execution': None,
                                                   'key': row['key'],
                                                   'reason': 'wedged_round_recycled'})
    for r in freed:   # post-commit: 终态先提交、文件后动（仓库纪律）
        store._release_locks([r['resource']], r['execution_id'], r['token'])
    # Pure orphans: terminal-execution leases on items this pass did not
    # recycle (item already terminal, release failed). Same terminal
    # definition as the wedge query (review F1 — one definition, no drift),
    # deterministic order, bounded.
    leaked = store.db.execute(
        """SELECT l.resource, l.execution_id, l.token FROM leases l
           JOIN executions e ON e.id = l.execution_id
           WHERE e.state NOT IN ('RUNNING','VERIFYING')
           ORDER BY e.created_at, l.resource LIMIT ?""",
        (limit * 4,)).fetchall()
    if leaked:
        with store.transaction():
            store.db.executemany(
                'DELETE FROM leases WHERE resource=? AND execution_id=?',
                [(r['resource'], r['execution_id']) for r in leaked])
        for row in leaked:
            store._release_locks([row['resource']], row['execution_id'],
                                 row['token'])
        leases += len(leaked)
    if items or leases:
        store.event('operator.hygiene', {'items': items, 'leases': leases})
    return {'items': items, 'leases': leases}


def drain_obsolete_readys(store, stale_after, limit=20, now=None):
    """Find READY operator items unconnected to a live command and re-offer
    them (issue #302 defect 3 — the ledger floor's readdrain path).

    A signal that fired while a round was already live, or whose dispatch
    FAILED before claim, leaves its item READY with no PENDING/CLAIMED/SPAWNED
    command. ``active_signal`` sees that READY forever and mutes the key
    (``skipped_active``), with nothing in the store ever re-deriving it.
    This scan re-enqueues those orphans so the serve consumer's dispatch offer
    can pick them up again. Bounded: by the ``limit`` per pass and the
    ``stale_after`` age gate (only items aged past the gate, so a fresh signal
    is never yanked mid-claim).
    """
    now = time.time() if now is None else now
    threshold = now - float(stale_after)
    rows = store.db.execute(
        """SELECT w.id, w.rowid
             FROM work_items w
            WHERE w.kind='operator' AND w.state='READY'
              AND w.created_at <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM commands c
                   WHERE c.number = w.rowid
                     AND c.state IN ('PENDING','CLAIMED','SPAWNED'))
            ORDER BY w.created_at, w.rowid
            LIMIT ?""",
        (threshold, limit)).fetchall()
    rearmed = 0
    for row in rows:
        enqueued, created = store.enqueue_command('operator_round', number=row['rowid'])
        if created:
            rearmed += 1
            store.event('operator.rearmed', {'item': row['id'], 'number': row['rowid']})
    return rearmed
