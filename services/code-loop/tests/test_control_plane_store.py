import json
import multiprocessing
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.cp.runtime import version
from codeloop.cp.store import Store


def race_claim(path, output):
    store = Store(path)
    value = store.claim('handler', 'racer')
    output.put(None if value is None else value['id'])
    store.db.close()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def item(self, key='issue-1', resources=None, version='v1'):
        return self.store.put_item(key, 'handler', version, {'number': 1}, resources or ['issue-1'])

    def lock(self, resource):
        return Path(self.temp.name) / 'locks' / resource

    def test_dedupe_and_version(self):
        first = self.item()
        self.assertEqual(first['id'], self.item()['id'])
        self.item(version='v2')
        self.assertEqual(['CANCELLED', 'READY'], [x['state'] for x in self.store.items()])

    def test_claim_guard_renew_and_derived_terminal(self):
        item = self.item()
        execution = self.store.claim('handler', 'alice')
        self.assertEqual(item['id'], execution['item_id'])
        self.assertIsNone(self.store.claim('handler', 'bob'))
        with self.assertRaises(ValueError):
            self.store.guard(execution['id'], execution['token'] + 1)
        self.store.renew(execution['id'], execution['token'], ttl=60)
        self.assertGreater(execution['expires_at'], 0)
        self.store.add_evidence(execution['id'], execution['token'], 'review', {'decision': 'APPROVE'})
        self.assertEqual(1, len(self.store.evidence(execution['id'], 'review')))
        # No result yet: derivation refuses, nothing is terminal.
        self.assertIsNone(self.store.derive_terminal(execution['id']))
        self.store.db.execute('UPDATE executions SET result=? WHERE id=?',
                              (json.dumps({'decision': 'APPROVE'}), execution['id']))
        self.assertIsNotNone(self.store.derive_terminal(execution['id']))
        self.assertFalse(self.lock('issue-1').exists())
        with self.assertRaises(ValueError):
            self.store.guard(execution['id'], execution['token'])

    def test_expired_execution_writes_late_until_stolen(self):
        self.item()
        execution = self.store.claim('handler', 'alice', ttl=0.01)
        time.sleep(0.03)
        # The clock no longer closes write channels: an unstealable-but-slow
        # worker can always finish.
        self.store.add_evidence(execution['id'], execution['token'], 'test', {'late': True})
        renewed = self.store.renew(execution['id'], execution['token'], ttl=3600)
        self.assertGreater(renewed['expires_at'], time.time())

    def test_directed_claim_reports_live_path_blocker(self):
        """issue #254: a directed claim that loses to a live path:* holder
        fills last_blocked with the blocker summary instead of a bare None."""
        self.item(key='issue-1', resources=['issue-1', 'path:*'])
        holder = self.store.claim('handler', 'alice')
        self.assertIsNotNone(holder)
        self.item(key='issue-2', resources=['issue-2', 'path:*'])
        refused = self.store.claim('handler', 'bob', item_id='issue-2')
        self.assertIsNone(refused)
        self.assertEqual(1, len(self.store.last_blocked))
        b = self.store.last_blocked[0]
        self.assertEqual('issue-1', b['item_key'])
        self.assertEqual('alice', b['worker'])
        self.assertEqual('RUNNING', b['state'])
        self.assertIn('path:*', b['resources'])
        self.assertFalse(b['stealable'])
        # a non-directed claim never reports blockers
        self.store.last_blocked = None
        self.assertIsNone(self.store.claim('handler', 'carol'))

    def test_capacity_full_claim_reports_live_holders(self):
        """A claim lost to full parallel slots names the holders (who, until
        when) instead of a bare None — the ai_vector review loop of 2026-09-30
        reported ten BLOCKED rounds because this path carried no facts."""
        for n in (1, 2, 3):
            self.item(key='issue-%d' % n, resources=['issue-%d' % n])
            self.assertIsNotNone(self.store.claim('handler', 'w%d' % n))
        self.item(key='issue-4', resources=['issue-4'])
        self.store.last_blocked = None
        self.assertIsNone(self.store.claim('handler', 'w4'))
        snapshot = self.store.last_blocked
        self.assertEqual(3, len(snapshot))
        self.assertEqual({'w1', 'w2', 'w3'}, {b['worker'] for b in snapshot})
        for b in snapshot:
            self.assertEqual('RUNNING', b['state'])
            self.assertGreater(b['expires_at'], time.time())
            self.assertFalse(b['stealable'])
        # A directed claim that loses to full slots gets the same facts.
        self.store.last_blocked = None
        self.assertIsNone(self.store.claim('handler', 'w4', item_id='issue-4'))
        self.assertEqual(3, len(self.store.last_blocked))
        # Expired leases are not capacity: a freed slot succeeds and the
        # success resets the snapshot instead of reporting stale holders.
        self.store.db.execute(
            "UPDATE executions SET expires_at=? WHERE worker='w1'",
            (time.time() - 1,))
        self.item(key='issue-5', resources=['issue-5'])
        self.store.last_blocked = None
        self.assertIsNotNone(self.store.claim('handler', 'w5'))
        self.assertIsNone(self.store.last_blocked)

    def test_legacy_store_without_last_seen_migrates_on_open(self):
        """AC7, the production upgrade path: a store created before the column
        reopens with last_seen_at added and existing rows carrying NULL (the
        ai_vector corpses must be collapse-eligible, not migrated away)."""
        with tempfile.TemporaryDirectory() as legacy_dir:
            db = sqlite3.connect(str(Path(legacy_dir) / 'control-plane.sqlite'))
            db.execute('''CREATE TABLE executions (
                id TEXT PRIMARY KEY, item_id TEXT NOT NULL, worker TEXT NOT NULL,
                token INTEGER NOT NULL UNIQUE, expires_at REAL NOT NULL,
                state TEXT NOT NULL, worktree TEXT, branch TEXT, head TEXT,
                result TEXT, item_version TEXT NOT NULL, created_at REAL NOT NULL)''')
            db.execute("INSERT INTO executions VALUES ('e1','i1','w',1,0,'RUNNING',"
                       "NULL,NULL,NULL,NULL,'v',1)")
            db.commit()
            db.close()
            store = Store(legacy_dir)
            try:
                self.assertIn('last_seen_at', {r[1] for r in store.db.execute(
                    'PRAGMA table_info(executions)')})
                self.assertIsNone(store.db.execute(
                    "SELECT last_seen_at FROM executions WHERE id='e1'").fetchone()[0])
            finally:
                store.db.close()

    def test_legacy_bound_rows_get_last_seen_refilled_on_migration(self):
        """review MAJOR-3: a legacy round that already bound a worktree is
        demonstrably alive (a corpse never reaches bind) — the migration must
        timestamp it now, so the first threshold computation does not read its
        old created_at as 2h+ silence and collapse a healthy long round."""
        with tempfile.TemporaryDirectory() as legacy_dir:
            db = sqlite3.connect(str(Path(legacy_dir) / 'control-plane.sqlite'))
            db.execute('''CREATE TABLE executions (
                id TEXT PRIMARY KEY, item_id TEXT NOT NULL, worker TEXT NOT NULL,
                token INTEGER NOT NULL UNIQUE, expires_at REAL NOT NULL,
                state TEXT NOT NULL, worktree TEXT, branch TEXT, head TEXT,
                result TEXT, item_version TEXT NOT NULL, created_at REAL NOT NULL)''')
            db.execute("INSERT INTO executions VALUES ('e1','i1','w',1,500,'RUNNING',"
                       "'/trees/t1','fix/x',NULL,NULL,'v',1)")
            db.commit()
            db.close()
            store = Store(legacy_dir)
            try:
                stamped = store.db.execute(
                    "SELECT last_seen_at FROM executions WHERE id='e1'").fetchone()[0]
                self.assertIsNotNone(stamped)
                self.assertGreater(stamped, time.time() - 10)
            finally:
                store.db.close()

    def test_store_tracks_last_seen_activity(self):
        self.assertIn('last_seen_at', {r[1] for r in self.store.db.execute(
            'PRAGMA table_info(executions)')})
        self.item()
        execution = self.store.claim('handler', 'alice')
        self.assertGreater(execution['last_seen_at'], 0)
        self.store.db.execute('UPDATE executions SET last_seen_at=? WHERE id=?',
                              (time.time() - 3600, execution['id']))
        self.store.guard(execution['id'], execution['token'])
        row = self.store.db.execute('SELECT last_seen_at FROM executions WHERE id=?',
                                    (execution['id'],)).fetchone()
        self.assertGreater(row[0], time.time() - 10)

    def test_collapse_scan_backed_by_indexes(self):
        """review MAJOR-2: executions(state,expires_at) and
        outbox(execution_id,state) indexes exist, so the collapse/sweep scans
        are range scans on state/expiry, not full table scans that grow
        linearly with the (append-only) executions table."""
        indexes = {r[0] for r in self.store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        self.assertIn('idx_executions_state_expires', indexes)
        self.assertIn('idx_outbox_execution_id', indexes)
        cols = {r[2] for r in self.store.db.execute(
            "PRAGMA index_info(idx_executions_state_expires)")}
        self.assertEqual({'state', 'expires_at'}, cols)

    def collapse(self, execution_id):
        return dict(self.store.db.execute(
            'SELECT state, expires_at FROM executions WHERE id=?',
            (execution_id,)).fetchone())

    def test_collapse_idle_reclaims_silent_corpses(self):
        """A round that died right after claim holds its slot for the whole
        lease; collapse_idle hands it to the standard sweep after the idle
        threshold instead (ai_vector: three reviewer corpses, 4h each)."""
        item1 = self.item()
        self.item(key='issue-2', resources=['issue-2'])
        legacy = self.store.claim('handler', 'alice')
        self.assertEqual(item1['id'], legacy['item_id'])
        # legacy row: last_seen_at NULL, created_at carries the age
        self.store.db.execute('UPDATE executions SET created_at=?, last_seen_at=NULL WHERE id=?',
                              (time.time() - 7201, legacy['id']))
        modern = self.store.claim('handler', 'bob')
        self.assertIsNotNone(modern)
        self.store.db.execute('UPDATE executions SET last_seen_at=? WHERE id=?',
                              (time.time() - 7201, modern['id']))
        self.assertEqual(sorted([legacy['id'], modern['id']]),
                         sorted(self.store.collapse_idle(7200)))
        for execution_id in (legacy['id'], modern['id']):
            self.assertEqual('CANCELLED', self.collapse(execution_id)['state'])
        self.assertEqual('READY', self.store.db.execute(
            'SELECT state FROM work_items WHERE id=?', (item1['id'],)).fetchone()[0])
        self.assertEqual(0, self.store.db.execute(
            'SELECT COUNT(*) FROM leases WHERE execution_id=?',
            (legacy['id'],)).fetchone()[0])
        self.assertFalse(self.lock('issue-1').exists())
        self.assertEqual(2, self.store.db.execute(
            "SELECT COUNT(*) FROM events WHERE kind='execution.swept'").fetchone()[0])

    def test_collapse_idle_spares_limbo_active_and_expired(self):
        self.item()
        self.item(key='issue-2', resources=['issue-2'])
        # limbo: an attempted publication is operator territory, never auto-cancelled
        limbo = self.store.claim('handler', 'alice')
        self.store.db.execute(
            'UPDATE executions SET created_at=?, last_seen_at=NULL WHERE id=?',
            (time.time() - 9_999, limbo['id']))
        self.store.db.execute(
            "INSERT INTO outbox (id, execution_id, action, payload, state) "
            "VALUES ('op-1', ?, 'review_comment', '{}', 'PENDING')", (limbo['id'],))
        # active: a live round's last_seen_at beats its backdated created_at
        active = self.store.claim('handler', 'bob')
        self.store.db.execute('UPDATE executions SET created_at=?, last_seen_at=NULL WHERE id=?',
                              (time.time() - 9_999, active['id']))
        self.store.guard(active['id'], active['token'])
        # expired: already sweep territory, outside collapse's contract
        self.item(key='issue-3', resources=['issue-3'])
        expired = self.store.claim('handler', 'carol', ttl=0.01)
        time.sleep(0.03)
        self.assertEqual([], self.store.collapse_idle(7200))
        self.assertEqual('RUNNING', self.collapse(limbo['id'])['state'])
        self.assertEqual('RUNNING', self.collapse(active['id'])['state'])
        self.assertEqual('RUNNING', self.collapse(expired['id'])['state'])

    def test_collapse_idle_derives_delivered_report(self):
        """D4 holds through collapse: a silent round that did deliver its
        report (clean outbox) derives SUCCEEDED, not a spurious cancel."""
        self.item()
        execution = self.store.claim('handler', 'alice')
        self.store.db.execute(
            'UPDATE executions SET created_at=?, last_seen_at=NULL, result=? WHERE id=?',
            (time.time() - 7201, json.dumps({'decision': 'APPROVE'}), execution['id']))
        self.assertEqual([execution['id']], self.store.collapse_idle(7200))
        self.assertEqual('SUCCEEDED', self.collapse(execution['id'])['state'])

    def test_claim_steals_expired_conflict_atomically(self):
        # Cross-item resource conflict: an expired execution holding branch-x
        # must not wedge a READY candidate that needs branch-x.
        victim_item = self.item(key='issue-1', resources=['issue-1', 'branch-x'])
        self.item(key='issue-2', resources=['issue-2', 'branch-x'])
        victim = self.store.claim('handler', 'alice', ttl=0.01)
        self.assertEqual(victim_item['id'], victim['item_id'])
        time.sleep(0.03)
        thief = self.store.claim('handler', 'bob')
        self.assertIsNotNone(thief)
        self.assertEqual('issue-2', self.store.db.execute(
            'SELECT key FROM work_items WHERE id=?', (thief['item_id'],)).fetchone()[0])
        states = {r['id']: r['state'] for r in self.store.db.execute(
            'SELECT id, state FROM executions').fetchall()}
        self.assertEqual('CANCELLED', states[victim['id']])
        self.assertEqual('RUNNING', states[thief['id']])
        self.assertGreater(thief['token'], victim['token'])
        self.assertEqual(0, self.store.db.execute(
            'SELECT COUNT(*) FROM leases WHERE execution_id=?', (victim['id'],)).fetchone()[0])
        kinds = [r['kind'] for r in self.store.db.execute(
            "SELECT kind FROM events WHERE kind='execution.stolen'")]
        self.assertEqual(['execution.stolen'], kinds)
        # The victim is fenced: late writes are refused after the steal.
        with self.assertRaises(ValueError):
            self.store.renew(victim['id'], victim['token'], ttl=60)
        # The victim's own item re-queued for an automatic re-run (no
        # successor version exists).
        self.assertEqual('READY', self.store.db.execute(
            'SELECT state FROM work_items WHERE id=?', (victim_item['id'],)).fetchone()['state'])

    def test_expired_victim_item_cancels_when_successor_ready(self):
        item = self.item(version='v1')
        victim = self.store.claim('handler', 'alice', ttl=0.01)
        self.item(version='v2')  # sync produced a newer READY successor
        time.sleep(0.03)
        self.store.sweep_expired()
        states = {r['version']: r['state'] for r in self.store.items()}
        self.assertEqual('CANCELLED', states['v1'])
        self.assertEqual('READY', states['v2'])

    def test_sweep_derives_completed_expired_execution(self):
        item = self.item()
        execution = self.store.claim('handler', 'alice')
        self.store.db.execute('UPDATE executions SET result=? WHERE id=?',
                              (json.dumps({'decision': 'APPROVE', 'reviewer': 'bob'}), execution['id']))
        self.store.db.execute('UPDATE executions SET expires_at=? WHERE id=?',
                              (time.time() - 1, execution['id']))
        self.assertEqual([execution['id']], self.store.sweep_expired())
        self.assertEqual('SUCCEEDED', self.store.db.execute(
            'SELECT state FROM executions WHERE id=?', (execution['id'],)).fetchone()['state'])
        self.assertEqual('SUCCEEDED', self.store.db.execute(
            'SELECT state FROM work_items WHERE id=?', (item['id'],)).fetchone()['state'])
        self.assertEqual(0, self.store.db.execute('SELECT COUNT(*) FROM leases').fetchone()[0])
        self.assertFalse(self.lock('issue-1').exists())
        # Idempotent: a second sweep has nothing left.
        self.assertEqual([], self.store.sweep_expired())

    def test_sweep_cancels_and_requeues_incomplete_expired_execution(self):
        item = self.item()
        execution = self.store.claim('handler', 'alice')
        self.store.db.execute('UPDATE executions SET expires_at=? WHERE id=?',
                              (time.time() - 1, execution['id']))
        self.assertEqual([execution['id']], self.store.sweep_expired())
        self.assertEqual('CANCELLED', self.store.db.execute(
            'SELECT state FROM executions WHERE id=?', (execution['id'],)).fetchone()['state'])
        self.assertEqual('READY', self.store.db.execute(
            'SELECT state FROM work_items WHERE id=?', (item['id'],)).fetchone()['state'])
        # The re-queued item is claimable again (the automatic re-run).
        again = self.store.claim('handler', 'bob')
        self.assertIsNotNone(again)
        self.assertGreater(again['token'], execution['token'])

    def test_sweep_never_touches_unresolved_publication(self):
        item = self.item()
        execution = self.store.claim('handler', 'alice')
        self.store.db.execute(
            "INSERT INTO outbox VALUES ('op1', ?, 'create_issue', '{}', 'SENDING', NULL)",
            (execution['id'],))
        self.store.db.execute('UPDATE executions SET expires_at=? WHERE id=?',
                              (time.time() - 1, execution['id']))
        self.assertEqual([], self.store.sweep_expired())
        self.assertEqual('RUNNING', self.store.db.execute(
            'SELECT state FROM executions WHERE id=?', (execution['id'],)).fetchone()['state'])
        # The limbo execution keeps resource exclusivity (fencing red line).
        self.item(key='issue-1', version='v2')
        self.assertIsNone(self.store.claim('handler', 'bob'))
        # But it does not eat parallel slots: another item claims fine.
        self.item(key='issue-2', resources=['issue-2'])
        self.assertIsNotNone(self.store.claim('handler', 'carol'))

    def test_reconcile_resolves_limbo_and_derives_terminal(self):
        item = self.item()
        execution = self.store.claim('handler', 'alice')
        result = {'decision': 'APPROVE', 'reviewer': 'bob', 'summary': 'done',
                  'action': 'close_issue', 'payload': {'number': 1}}
        self.store.db.execute('UPDATE executions SET result=? WHERE id=?',
                              (json.dumps(result), execution['id']))
        op = version([execution['id'], result], {})
        self.store.db.execute(
            "INSERT INTO outbox VALUES (?, ?, 'close_issue', '{}', 'UNKNOWN', NULL)", (op, execution['id']))
        self.store.db.execute('UPDATE executions SET expires_at=? WHERE id=?',
                              (time.time() - 1, execution['id']))
        # Limbo: neither sweep nor claim may terminalize it.
        self.assertEqual([], self.store.sweep_expired())
        self.item(key='issue-1', version='v2')
        self.assertIsNone(self.store.claim('handler', 'bob'))
        # Operator confirms the remote receipt: derivation closes the round.
        self.store.reconcile(op, {'number': 42})
        self.assertEqual('SUCCEEDED', self.store.db.execute(
            'SELECT state FROM executions WHERE id=?', (execution['id'],)).fetchone()['state'])
        self.assertEqual('SUCCEEDED', self.store.db.execute(
            'SELECT state FROM work_items WHERE id=?', (item['id'],)).fetchone()['state'])
        self.assertEqual(0, self.store.db.execute('SELECT COUNT(*) FROM leases').fetchone()[0])
        self.assertFalse(self.lock('issue-1').exists())

    def test_finalize_requires_sent_publication_for_action_reports(self):
        item = self.item()
        execution = self.store.claim('handler', 'alice')
        result = {'decision': 'APPROVE', 'reviewer': 'bob', 'summary': 'done',
                  'action': 'close_issue', 'payload': {'number': 1}}
        self.store.db.execute('UPDATE executions SET result=? WHERE id=?',
                              (json.dumps(result), execution['id']))
        # Action requested but its publication row does not exist / is not
        # SENT: the obligations are not proven, derivation refuses.
        self.assertIsNone(self.store.derive_terminal(execution['id']))
        self.assertEqual('RUNNING', self.store.db.execute(
            'SELECT state FROM executions WHERE id=?', (execution['id'],)).fetchone()['state'])
        op = version([execution['id'], result], {})
        self.store.db.execute(
            "INSERT INTO outbox VALUES (?, ?, 'close_issue', '{}', 'SENT', '{}')", (op, execution['id']))
        self.assertIsNotNone(self.store.derive_terminal(execution['id']))
        self.assertEqual('SUCCEEDED', self.store.db.execute(
            'SELECT state FROM work_items WHERE id=?', (item['id'],)).fetchone()['state'])

    def test_sweep_ignores_live_executions(self):
        self.item()
        execution = self.store.claim('handler', 'alice')
        self.store.db.execute('UPDATE executions SET result=? WHERE id=?',
                              (json.dumps({'decision': 'APPROVE'}), execution['id']))
        self.assertEqual([], self.store.sweep_expired())
        self.assertEqual('RUNNING', self.store.db.execute(
            'SELECT state FROM executions WHERE id=?', (execution['id'],)).fetchone()['state'])

    def test_path_and_unknown_conflicts(self):
        self.item(resources=['issue-1', 'path:skills/a'])
        one = self.store.claim('handler', 'alice')
        self.item(key='issue-2', resources=['issue-2', 'path:skills/a/sub'])
        self.assertIsNone(self.store.claim('handler', 'bob'))
        self.item(key='issue-3', resources=['issue-3', 'path:skills/ab'])
        self.assertIsNotNone(self.store.claim('handler', 'carol'))
        self.item(key='issue-4', resources=['issue-4', 'path:*'])
        self.assertIsNone(self.store.claim('handler', 'dave'))
        self.assertIsNotNone(one)

    def test_path_star_does_not_block_object_resources(self):
        # A RUNNING handler with an unknown path scope must not wedge
        # reviewer/discover claims: path resources only exclude path work.
        self.item(resources=['issue-1', 'path:*'])
        self.assertIsNotNone(self.store.claim('handler', 'alice'))
        self.store.put_item('review:2', 'reviewer', 'v1', {'number': 2},
                            ['pr-2', 'branch-feature/x'])
        review = self.store.claim('reviewer', 'rev1')
        self.assertIsNotNone(review)
        self.store.put_item('discover', 'discover', 'v1', {'task': 'DISCOVERY'},
                            ['issue-discover'])
        self.assertIsNotNone(self.store.claim('discover', 'disc1'))
        # Path-scoped candidates still wait: the unknown scope may overlap.
        self.item(key='issue-9', resources=['issue-9', 'path:skills/a'])
        self.assertIsNone(self.store.claim('handler', 'bob'))
        # Both sides wildcard stays the degenerate exclusive case.
        self.item(key='issue-10', resources=['issue-10', 'path:*'])
        self.assertIsNone(self.store.claim('handler', 'carol'))

    def test_object_resources_do_not_block_path_claims(self):
        self.store.put_item('review:3', 'reviewer', 'v1', {'number': 3},
                            ['pr-3', 'branch-feature/y'])
        self.assertIsNotNone(self.store.claim('reviewer', 'rev1'))
        self.item(key='issue-4', resources=['issue-4', 'path:*'])
        self.assertIsNotNone(self.store.claim('handler', 'alice'))

    def test_unknown_lock_owner_is_preserved(self):
        lock = self.lock('pr-1')
        lock.mkdir(parents=True)
        self.item(resources=['issue-1', 'pr-1'])
        self.assertIsNone(self.store.claim('handler', 'alice'))
        self.assertFalse(self.lock('issue-1').exists())
        self.assertTrue(lock.exists())
        # A corrupt owner file is the same conservative skip.
        corrupt = self.lock('pr-2')
        corrupt.mkdir(parents=True)
        (corrupt / 'agent.json').write_text('not-json', encoding='utf-8')
        self.item(key='issue-9', resources=['issue-9', 'pr-2'])
        self.assertIsNone(self.store.claim('handler', 'alice'))
        self.assertTrue(corrupt.exists())

    def test_dead_owner_lock_is_taken_over(self):
        self.item()
        execution = self.store.claim('handler', 'alice')
        self.store.db.execute('UPDATE executions SET result=? WHERE id=?',
                              (json.dumps({'decision': 'APPROVE'}), execution['id']))
        self.store.derive_terminal(execution['id'])
        # Simulate a crash leftover: the terminal owner's lock file persists.
        leftover = self.lock('issue-1')
        leftover.mkdir()
        (leftover / 'agent.json').write_text(json.dumps(
            {'execution_id': execution['id'], 'token': execution['token']}), encoding='utf-8')
        # A new READY candidate on the same resource reclaims the dead lock.
        successor = self.item(version='v2')
        second = self.store.claim('handler', 'bob')
        self.assertIsNotNone(second)
        self.assertEqual(successor['id'], second['item_id'])
        owner = json.loads((leftover / 'agent.json').read_text(encoding='utf-8'))
        self.assertEqual(second['id'], owner['execution_id'])

    def test_partial_acquisition_cleanup(self):
        self.item(resources=['issue-1', 'pr-1'])
        # issue-1 acquires, then an unknown-owner pr-1 lock blocks the rest:
        # the partial acquisition must not leave issue-1 behind.
        pr_lock = self.lock('pr-1')
        pr_lock.mkdir(parents=True)
        self.assertIsNone(self.store.claim('handler', 'alice'))
        self.assertFalse(self.lock('issue-1').exists())
        self.assertTrue(pr_lock.exists())

    def test_concurrent_claim(self):
        self.item()
        ctx = multiprocessing.get_context('spawn')
        output = ctx.Queue()
        processes = [ctx.Process(target=race_claim, args=(self.temp.name, output)) for _ in range(2)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            self.assertEqual(0, process.exitcode)
        claims = [output.get(timeout=2) for _ in processes]
        self.assertEqual(1, sum(x is not None for x in claims))

    def test_monotonic_tokens_limit_and_selector(self):
        self.item()
        first = self.store.claim('handler', 'alice', limit=1)
        self.item(key='issue-2', resources=['issue-2'])
        # limit counts live leases only; alice is alive so bob is refused.
        self.assertIsNone(self.store.claim('handler', 'bob', limit=1))
        self.item(version='v2')
        # Complete alice by derivation, then claim by key selector.
        self.store.db.execute('UPDATE executions SET result=? WHERE id=?',
                              (json.dumps({'decision': 'APPROVE'}), first['id']))
        self.store.derive_terminal(first['id'])
        second = self.store.claim('handler', 'bob', limit=1, item_id='issue-1')
        self.assertIsNotNone(second)
        self.assertGreater(second['token'], first['token'])

    def test_release_never_removes_changed_owner(self):
        self.item()
        execution = self.store.claim('handler', 'alice')
        owner = self.lock('issue-1') / 'agent.json'
        owner.write_text(json.dumps({'execution_id': 'other', 'token': execution['token']}))
        self.store.db.execute('UPDATE executions SET result=? WHERE id=?',
                              (json.dumps({'decision': 'APPROVE'}), execution['id']))
        self.store.derive_terminal(execution['id'])
        self.assertTrue(owner.exists())

    def test_reject_invalid_resources_and_ttl(self):
        for resource in ['path:../x', 'path:/absolute', 'path:a/../b', '../issue-1']:
            with self.assertRaises(ValueError):
                self.item(resources=[resource])
        self.item()
        for ttl in [0, -1, float('nan'), float('inf'), True]:
            with self.assertRaises(ValueError):
                self.store.claim('handler', 'alice', ttl=ttl)

    def test_put_item_participates_in_snapshot_transaction(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.item()
                self.store.event('snapshot', {})
                raise RuntimeError('abort snapshot')
        self.assertEqual([], self.store.items())
        self.assertEqual(0, self.store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0])

    def test_transaction_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.store.db.execute("INSERT INTO meta VALUES ('rollback','x')")
                raise RuntimeError('stop')
        self.assertIsNone(self.store.db.execute("SELECT * FROM meta WHERE key='rollback'").fetchone())


if __name__ == '__main__':
    unittest.main()
