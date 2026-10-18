"""Dashboard mirror of the operator store (operator_signals/operator_runs)
and its collection path (spec docs/specs/2026-09-30-cp-operator-agent.md)."""
import json
import sqlite3
import sys as _sys
import tempfile
import time
import unittest
from pathlib import Path

for _p in (str(Path(__file__).resolve().parents[1]),
           str(Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.dash import collect, schema
from codeloop.operator import store as op_store


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='op-mirror-')
        self.db = schema.connect(Path(self.home) / 'dash.sqlite')

    def tearDown(self):
        self.db.close()

    def test_mirror_tables_exist(self):
        tables = {r[0] for r in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn('operator_signals', tables)
        self.assertIn('operator_runs', tables)

    def test_collect_operator_absent_store_is_noop(self):
        collect.collect_operator(self.db, home=self.home)
        self.assertEqual(self.db.execute(
            'SELECT count(*) FROM operator_signals').fetchone()[0], 0)

    def test_collect_operator_mirrors_signals_and_runs(self):
        store = op_store.open_store(self.home)
        try:
            item, _ = op_store.put_signal(store, source='alert', kind='loop_stall',
                                          key='k1', payload={'message': 'stall'},
                                          org_repo='mocx__skills', severity='high',
                                          now=1_000.0)
            store.db.execute('''INSERT INTO executions (id,item_id,worker,token,
                expires_at,state,worktree,branch,head,result,item_version,created_at)
                VALUES ('e1',?,'DESKTOP-MOCX/operator',1,?,'RUNNING',
                NULL,NULL,NULL,NULL,'v',?)''',
                (item['id'], time.time() + 999.0, time.time()))
            collect.collect_operator(self.db, home=self.home)
            sig = self.db.execute('SELECT * FROM operator_signals').fetchone()
            self.assertEqual(sig['id'], item['id'])
            self.assertEqual(sig['kind'], 'loop_stall')
            self.assertEqual(sig['org_repo'], 'mocx__skills')
            self.assertEqual(json.loads(sig['payload'])['message'], 'stall')
            run = self.db.execute('SELECT * FROM operator_runs').fetchone()
            self.assertEqual(run['id'], 'e1')
            self.assertEqual(run['signal_id'], item['id'])
            self.assertEqual(run['state'], 'RUNNING')
            # idempotent re-run: no duplicates
            collect.collect_operator(self.db, home=self.home)
            self.assertEqual(self.db.execute(
                'SELECT count(*) FROM operator_signals').fetchone()[0], 1)
        finally:
            store.db.close()

    def test_collect_operator_prunes_rows_outside_window(self):
        """Rows older than the window are pruned, not retained forever
        (review MINOR-2: the mirror used to DELETE+INSERT the whole history).

        created_at is wall-clock (store.put_item stamps time.time()), so an
        old row is laid down directly in the source store with a created_at
        outside the mirror window."""
        store = op_store.open_store(self.home)
        try:
            old = time.time() - 60 * 86400
            item, _ = op_store.put_signal(
                store, source='alert', kind='loop_stall', key='old1',
                payload={'message': 'old'}, org_repo='mocx__skills',
                severity='low', now=old)
            store.db.execute(
                'UPDATE work_items SET created_at=? WHERE id=?',
                (old, item['id']))
            store.db.commit()
            collect.collect_operator(self.db, home=self.home)
            row = self.db.execute(
                'SELECT * FROM operator_signals').fetchone()
            self.assertIsNone(row, 'row outside the window must be pruned')
        finally:
            store.db.close()

    def test_collect_operator_upserts_updated_signal(self):
        """A signal re-recorded in the source store (same key, superseded id
        per put_item) upserts by id: re-collect must never duplicate any row
        (review MINOR-2 replaced the DELETE+INSERT rebuild with id-merged
        upserts, so stable windows accumulate without loss or duplication)."""
        store = op_store.open_store(self.home)
        try:
            item, _ = op_store.put_signal(
                store, source='alert', kind='loop_stall', key='u1',
                payload={'message': 'v1'}, org_repo='mocx__skills',
                severity='high', now=2_000.0)
            # put_signal supersedes on re-fire: the same key gets a fresh item
            # id (the first one is CANCELLED). The mirror keeps *all* windowed
            # rows (state included) so dashboard queries see the transition.
            item2, _ = op_store.put_signal(
                store, source='alert', kind='loop_stall', key='u1',
                payload={'message': 'v2'}, org_repo='mocx__skills',
                severity='high', now=2_001.0)
            collect.collect_operator(self.db, home=self.home)
            rows = dict((r['id'], r) for r in self.db.execute(
                'SELECT * FROM operator_signals'))
            self.assertEqual(set(rows), {item['id'], item2['id']})
            self.assertEqual(
                json.loads(rows[item2['id']]['payload'])['message'], 'v2')
            # idempotent re-run keeps the same two rows — no duplication
            collect.collect_operator(self.db, home=self.home)
            self.assertEqual(self.db.execute(
                'SELECT count(*) FROM operator_signals').fetchone()[0], 2)
        finally:
            store.db.close()


class RunDetailOperatorTests(unittest.TestCase):
    def test_operator_deep_link_reads_operator_store(self):
        from unittest import mock
        from codeloop.dash import runapi
        home = tempfile.mkdtemp(prefix='op-run-')
        store = op_store.open_store(home)
        try:
            item, _ = op_store.put_signal(store, source='alert', kind='loop_stall',
                                          key='k1', payload={'message': 'stall'},
                                          org_repo='m', now=1_000.0)
            claim = store.claim('operator', worker='H/operator', ttl=1800,
                                item_id=item['id'])
            log = op_store.state_dir(home) / 'executions' / (claim['id'] + '.log')
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text('hello operator\n', encoding='utf-8')
            with mock.patch.object(runapi, '_operator_state_dir',
                                   lambda: op_store.state_dir(home)):
                payload = runapi.run_detail(None, {'id': 1, 'config_path': ''},
                                            'operator:' + claim['id'], since=0)
            self.assertIsNotNone(payload)
            self.assertEqual(payload['execution']['id'], claim['id'])
            self.assertEqual(payload['item']['key'], 'op:alert:k1')
            self.assertEqual(payload['session']['bound'], 'spawn-log')
            texts = [e['text'] for e in payload['session']['events']]
            self.assertIn('hello operator', texts)
            self.assertTrue(any(e['kind'] == 'execution.claimed'
                                for e in payload['timeline']))
            # unknown exec → None (HTTP 404)
            with mock.patch.object(runapi, '_operator_state_dir',
                                   lambda: op_store.state_dir(home)):
                self.assertIsNone(runapi.run_detail(
                    None, {'id': 1, 'config_path': ''},
                    'operator:' + '0' * 32, since=0))
        finally:
            store.db.close()

    def test_operator_deep_link_falls_back_to_signal_id(self):
        """MAJOR-3: the board's ledger and manual triggers carry the signal
        (work_item) id — view=run must resolve it to the newest execution of
        the signal instead of 404ing on a different uuid. issue #301 defect 2:
        the resolved execution id must drive the log/evidence/timeline reads —
        a signal deep link must render the round's session flow, not bind to a
        nonexistent <item_id>.log."""
        from unittest import mock
        from codeloop.dash import runapi
        home = tempfile.mkdtemp(prefix='op-run2-')
        store = op_store.open_store(home)
        try:
            item, _ = op_store.put_signal(
                store, source='alert', kind='loop_stall', key='s1',
                payload={'message': 'stall'}, org_repo='m', now=1_000.0)
            claim = store.claim('operator', worker='H/operator', ttl=1800,
                                item_id=item['id'])
            log = op_store.state_dir(home) / 'executions' / (claim['id'] + '.log')
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text('round log\n', encoding='utf-8')
            with mock.patch.object(runapi, '_operator_state_dir',
                                   lambda: op_store.state_dir(home)):
                payload = runapi.run_detail(None, {'id': 1, 'config_path': ''},
                                            'operator:' + item['id'], since=0)
            self.assertIsNotNone(payload, 'signal id must resolve to its round')
            self.assertEqual(payload['execution']['id'], claim['id'])
            self.assertEqual(payload['item']['key'], 'op:alert:s1')
            # issue #301: the session log is keyed by execution id — the deep
            # link must bind to it even when the URL carried the signal id.
            self.assertEqual(payload['session']['bound'], 'spawn-log')
            texts = [e['text'] for e in payload['session']['events']]
            self.assertIn('round log', texts)
        finally:
            store.db.close()


class CollectWiringTests(unittest.TestCase):
    def test_collect_runs_operator_branch_without_store(self):
        home = tempfile.mkdtemp(prefix='op-wire-')
        db = schema.connect(Path(home) / 'dash.sqlite')
        try:
            summary = collect.collect(db, root=home, db_path=str(
                Path(home) / 'dash.sqlite'))
            self.assertEqual(summary['failed'], [])
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
