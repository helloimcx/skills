#!/usr/bin/env python3
"""The FAILED-ack guard in the command ledger (issue #235/#255): a FAILED ack
must not overwrite a finished round. The guard reads the execution the ack
body carries — a publish landing inside the watcher's confirm window leaves
the command SPAWNED, and a command stuck in CLAIMED whose SPAWNED ack went
missing is the same CAS loss. Split from test_cp_commands.py (800-line red
line, issue #185 convention: split, don't raise)."""
import tempfile
import unittest
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.cp.store import Store


def make_store(tmp):
    return Store(str(Path(tmp) / 'cp.sqlite'))


class FailedAckGuardTests(unittest.TestCase):
    """A FAILED ack whose execution is terminal is a CAS loss and must leave
    the command untouched — whether the row is SPAWNED (issue #235) or stuck
    in CLAIMED with a lost SPAWNED ack (issue #255)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_store(self.tmp.name)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_failed_ack_guard_covers_claimed_with_ack_execution(self):
        """issue #255: the FAILED ack guard must also cover a command stuck in
        CLAIMED whose SPAWNED ack went missing (execution=NULL on the row) —
        the ack body still carries the execution the claim started, so a
        terminal execution is a CAS loss there too."""
        row, _ = self.store.enqueue_command('review_round', 89, now=1000.0)
        self.store.mark_dispatched(row['id'], 'm1', now=1001.0)
        # SPAWNED ack lost: command stays CLAIMED, row execution is NULL
        self.store.db.execute(
            "INSERT INTO work_items(id,key,kind,version,payload,resources,state,"
            "priority,created_at) VALUES ('item-2','review:89','reviewer','v1','{}',"
            "'[]','READY',0,1000.0)")
        self.store.db.execute(
            "INSERT INTO executions(id,item_id,worker,token,expires_at,state,"
            "item_version,created_at) VALUES ('exec-2','item-2','m1',7,99999.0,"
            "'SUCCEEDED','v1',1000.0)")
        self.store.db.commit()
        # round finished -> the FAILED ack (carrying the claim's execution) is a
        # CAS loss, command stays CLAIMED
        self.assertIsNone(self.store.ack_command(
            row['id'], 'm1', 'FAILED', fail_reason='PROCESS_DIED: session process exited',
            now=1003.0, execution='exec-2'))
        self.assertEqual('CLAIMED', self.store.latest_command('review_round', 89)['state'])
        # a still-running execution still accepts the FAILED ack
        self.store.db.execute("UPDATE executions SET state='RUNNING' WHERE id='exec-2'")
        self.store.db.commit()
        acked = self.store.ack_command(
            row['id'], 'm1', 'FAILED', fail_reason='PROCESS_DIED: session process exited',
            now=1004.0, execution='exec-2')
        self.assertIsNotNone(acked)
        self.assertEqual('FAILED', acked['state'])


if __name__ == '__main__':
    unittest.main()
