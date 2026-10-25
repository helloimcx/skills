"""Worker loop stall edges use durable project events and recover on progress."""
import json
import sys
from pathlib import Path

for _p in (str(Path(__file__).resolve().parents[1]),
           str(Path(__file__).resolve().parents[1] / 'scripts'),
           str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _service_test_base import ServiceTestsBase


class WorkerStallEventTests(ServiceTestsBase):
    def test_discover_stall_emits_once_and_resolves_after_a_new_claim(self):
        self.s.sync()
        execution = self.s.claim('discover', 'worker-1/discover')
        now = 200000.0
        self.s.store.db.execute(
            'UPDATE executions SET created_at=?,expires_at=? WHERE id=?',
            (now - 64801, now - 1, execution['id']))
        self.s.store.db.commit()
        machine = {'id': 'machine-1', 'name': 'worker-1', 'enabled': 1,
                   'revoked': 0, 'granted': True}

        self.s.evaluate_worker_stalls([machine], threshold=64800, now=now)
        rows = self.s.store.db.execute(
            "SELECT kind,payload FROM events WHERE kind LIKE 'worker.loop_%' "
            'ORDER BY id').fetchall()
        self.assertEqual(['worker.loop_stalled'], [row['kind'] for row in rows])
        payload = json.loads(rows[0]['payload'])
        self.assertEqual(('machine-1', 'a__b', 'discover'),
                         (payload['machine_id'], payload['project'], payload['loop']))
        self.assertEqual(execution['id'], payload['last_execution_id'])

        self.s.evaluate_worker_stalls([machine], threshold=64800, now=now + 60)
        self.assertEqual(1, self.s.store.db.execute(
            "SELECT COUNT(*) FROM events WHERE kind='worker.loop_stalled'").fetchone()[0])

        self.s.store.db.execute(
            'UPDATE executions SET created_at=?,expires_at=? WHERE id=?',
            (now + 61, now + 3600, execution['id']))
        self.s.store.db.commit()
        self.s.evaluate_worker_stalls([machine], threshold=64800, now=now + 62)
        resolved = self.s.store.db.execute(
            "SELECT payload FROM events WHERE kind='worker.loop_resolved'").fetchall()
        self.assertEqual(1, len(resolved))
        self.assertEqual('discover', json.loads(resolved[0]['payload'])['loop'])


if __name__ == '__main__':
    import unittest
    unittest.main()
