"""Operator machine-level store: signal items, active-dedup lookup (spec
docs/specs/2026-09-30-cp-operator-agent.md — operator Store reuses the CP
Store schema in its own machine-global state dir)."""
import sys as _sys
import threading
import unittest
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.operator import store as op_store


class OperatorStoreTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.home = tempfile.mkdtemp(prefix='op-store-')

    def test_open_store_creates_machine_level_state(self):
        store = op_store.open_store(self.home)
        try:
            tables = {r[0] for r in store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({'work_items', 'executions', 'commands',
                             'progress_events', 'events'} <= tables)
            state = op_store.state_dir(self.home)
            self.assertEqual(state.name, 'state')
            self.assertEqual(state.parent.name, 'operator')
        finally:
            store.db.close()

    def test_operator_store_shared_across_threads_default(self):
        """BLOCKER-2: the operator store is shared across threads by design
        (serve-side consumer thread + HTTP handler threads on one instance) —
        open_store must default to check_same_thread=False; sqlite3 serializes
        access itself."""
        store = op_store.open_store(self.home)
        errors = []
        try:
            def poke():
                try:
                    store.db.execute(
                        "SELECT count(*) FROM work_items").fetchone()
                except Exception as exc:  # pragma: no cover - failure path
                    errors.append(exc)
            thread = threading.Thread(target=poke)
            thread.start()
            thread.join()
            self.assertEqual(errors, [])
        finally:
            store.db.close()

    def test_put_signal_creates_operator_item(self):
        store = op_store.open_store(self.home)
        try:
            item, created = op_store.put_signal(
                store, source='alert', kind='loop_stall', key='k|mocx__skills|handler',
                payload={'rule': 'loop_stall', 'message': 'stall'}, org_repo='mocx__skills',
                severity='high', now=1_000.0)
            self.assertTrue(created)
            row = store.db.execute("SELECT * FROM work_items WHERE id=?",
                                   (item['id'],)).fetchone()
            self.assertEqual(row['kind'], 'operator')
            self.assertEqual(row['key'], 'op:alert:k|mocx__skills|handler')
            import json
            payload = json.loads(row['payload'])
            self.assertEqual(payload['source'], 'alert')
            self.assertEqual(payload['kind'], 'loop_stall')
            self.assertEqual(payload['org_repo'], 'mocx__skills')
            self.assertEqual(payload['severity'], 'high')
            self.assertEqual(payload['message'], 'stall')
        finally:
            store.db.close()

    def test_active_signal_finds_ready_and_ignores_terminal(self):
        store = op_store.open_store(self.home)
        try:
            self.assertIsNone(op_store.active_signal(store, 'alert', 'k1'))
            op_store.put_signal(store, source='alert', kind='loop_stall', key='k1',
                                payload={}, now=1_000.0)
            self.assertIsNotNone(op_store.active_signal(store, 'alert', 'k1'))
            # A cancelled/terminal item is not "active": the signal may fire again.
            store.db.execute("UPDATE work_items SET state='CANCELLED' WHERE kind='operator'")
            self.assertIsNone(op_store.active_signal(store, 'alert', 'k1'))
        finally:
            store.db.close()

    def test_refired_signal_new_version_supersedes_ready_item(self):
        store = op_store.open_store(self.home)
        try:
            first, _ = op_store.put_signal(store, source='alert', kind='loop_stall',
                                           key='k1', payload={}, now=1_000.0)
            second, created = op_store.put_signal(store, source='alert', kind='loop_stall',
                                                  key='k1', payload={}, now=2_000.0)
            self.assertTrue(created)
            self.assertNotEqual(first['id'], second['id'])
            states = {r['id']: r['state'] for r in store.db.execute(
                "SELECT id,state FROM work_items WHERE key=?", ('op:alert:k1',))}
            self.assertEqual(states[first['id']], 'CANCELLED')
            self.assertEqual(states[second['id']], 'READY')
        finally:
            store.db.close()

    def test_signal_key_namespacing(self):
        self.assertEqual(op_store.signal_key('alert', 'x'),
                         op_store.signal_key('alert', 'x'))
        self.assertNotEqual(op_store.signal_key('alert', 'x'),
                            op_store.signal_key('cp_event', 'x'))

    def test_open_store_is_thread_shared(self):
        """MAJOR-2: the operator store is opened once on the serve main thread
        and executed from HTTP handler threads — the connection must be opened
        thread-shared (check_same_thread=False), matching review #289
        BLOCKER-2, or the resident subscription silently dies with
        ProgrammingError swallowed into the round result."""
        import threading
        store = op_store.open_store(self.home)
        try:
            errors = []

            def worker():
                try:
                    store.db.execute("SELECT COUNT(*) FROM work_items").fetchone()
                except Exception as exc:  # pragma: no cover - failure path
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
        finally:
            store.db.close()


if __name__ == '__main__':
    unittest.main()
