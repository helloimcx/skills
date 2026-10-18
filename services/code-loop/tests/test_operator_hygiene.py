"""issue #302 operator state-machine hygiene: zombie-recycle on fail/terminal
paths, orphan-READY cancel on dispatch fail, and the ledger-floor drain that
re-offers READY items whose command never went live (cap truncation /
dispatch failure). Each test pins one mechanism the issue named."""
import json
import sys as _sys
import tempfile
import unittest
from pathlib import Path as _Path
from unittest import mock

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts"),
           str(_Path(__file__).resolve().parents[1] / "scripts" / "..")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.operator import config as op_config
from codeloop.operator import face, store as op_store


def cfg(**over):
    base = op_config.load_operator_config({'operator': dict(enabled=True, **over)})
    return base


class FailRoundRecyclesParentTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='op-hygiene-')
        self.store = op_store.open_store(self.home)
        item, _ = op_store.put_signal(self.store, source='alert', kind='loop_stall',
                                      key='k1', payload={'message': 'stall'},
                                      org_repo='mocx__skills', now=1_000.0)
        self.claim = face.claim(self.store, worker='HOST/operator', ttl=1_800,
                                item_id=item['id'])

    def tearDown(self):
        self.store.db.close()

    def test_fail_round_recycles_parent_item(self):
        """PROCESS_DIED verdict must free the signal key: the item that was
        RUNNING (claimed) becomes CANCELLED, so active_signal no longer treats
        any same-key READY as live — a re-fired signal can claim a fresh item."""
        face.fail_round(self.store, self.claim['id'], self.claim['token'],
                        'PROCESS_DIED: exit=1')
        item = self.store.db.execute('SELECT * FROM work_items WHERE key=?',
                                     ('op:alert:k1',)).fetchone()
        self.assertEqual(item['state'], 'CANCELLED')
        self.assertIsNone(op_store.active_signal(self.store, 'alert', 'k1'))
        # the lease was dropped: a fresh signal for the same key claims cleanly
        fresh, created = op_store.put_signal(self.store, source='alert',
                                             kind='loop_stall', key='k1',
                                             payload={}, now=2_000.0)
        self.assertTrue(created)
        self.assertIsNotNone(op_store.active_signal(self.store, 'alert', 'k1'))

    def test_fail_round_after_terminal_report_keeps_item_succeeded(self):
        """BLOCKER-1 preserved: a watcher racing a self-reported SUCCEEDED
        round must not recycle the item — the report already finalised it."""
        face.report(self.store, self.claim['id'], self.claim['token'],
                    {'signal': 'op:alert:k1', 'diagnosis': 'd', 'actions': [],
                     'outcome': 'RESOLVED', 'evidence': []})
        item = self.store.db.execute('SELECT * FROM work_items WHERE key=?',
                                     ('op:alert:k1',)).fetchone()
        self.assertEqual(item['state'], 'SUCCEEDED')
        face.fail_round(self.store, self.claim['id'], self.claim['token'],
                        'PROCESS_DIED: exit=1')
        item = self.store.db.execute('SELECT * FROM work_items WHERE key=?',
                                     ('op:alert:k1',)).fetchone()
        self.assertEqual(item['state'], 'SUCCEEDED')


class DispatchFailCancelsOrphanTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='op-orphan-')
        self.store = op_store.open_store(self.home)

    def tearDown(self):
        self.store.db.close()

    def test_ack_failed_without_claim_cancels_orphan_ready(self):
        """Dispatch failure before claim (claim=None) must cancel the orphan
        READY item — otherwise active_signal sees a READY it will never reclaim
        and mutes the key permanently."""
        item, _ = op_store.put_signal(self.store, source='alert', kind='loop_stall',
                                      key='k1', payload={}, now=1_000.0)
        rowid = self.store.db.execute('SELECT rowid FROM work_items WHERE id=?',
                                      (item['id'],)).fetchone()['rowid']
        cmd, _ = self.store.enqueue_command('operator_round', number=rowid)
        self.store.mark_dispatched(cmd['id'], 'HOST')
        face.ack_failed(self.store, cmd['id'], 'HOST',
                        "TypeError: 'NoneType' object is not subscriptable",
                        claim=None)
        item = self.store.db.execute('SELECT * FROM work_items WHERE key=?',
                                     ('op:alert:k1',)).fetchone()
        self.assertEqual(item['state'], 'CANCELLED')
        self.assertIsNone(op_store.active_signal(self.store, 'alert', 'k1'))

    def test_ack_failed_with_claim_preserves_item(self):
        """A FAILED ack carrying a claim means the round DID start (item
        RUNNING) — recycle belongs to the watcher's fail_round, not this ack."""
        item, _ = op_store.put_signal(self.store, source='alert', kind='loop_stall',
                                      key='k1', payload={}, now=1_000.0)
        claim = face.claim(self.store, worker='HOST/operator', ttl=1_800,
                           item_id=item['id'])
        rowid = self.store.db.execute('SELECT rowid FROM work_items WHERE id=?',
                                      (item['id'],)).fetchone()['rowid']
        cmd, _ = self.store.enqueue_command('operator_round', number=rowid)
        self.store.mark_dispatched(cmd['id'], 'HOST')
        self.store.ack_command(cmd['id'], 'HOST', 'SPAWNED', execution=claim['id'])
        face.ack_failed(self.store, cmd['id'], 'HOST', 'OSError: boom', claim=claim)
        item = self.store.db.execute('SELECT * FROM work_items WHERE key=?',
                                     ('op:alert:k1',)).fetchone()
        self.assertEqual(item['state'], 'RUNNING')


class DrainObsoleteReadysTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='op-drain-')
        self.store = op_store.open_store(self.home)

    def tearDown(self):
        self.store.db.close()

    def seed_ready(self, key='k1', age=1_000.0):
        item, _ = op_store.put_signal(self.store, source='alert', kind='loop_stall',
                                      key='k1', payload={}, now=1_000.0)
        # put_item stamps created_at with wall-clock; force it to the past so
        # the age gate is deterministic.
        self.store.db.execute('UPDATE work_items SET created_at=? WHERE id=?',
                              (age, item['id']))
        rowid = self.store.db.execute('SELECT rowid FROM work_items WHERE id=?',
                                      (item['id'],)).fetchone()['rowid']
        # a dispatch fail leaves the item READY with a terminal command
        cmd, _ = self.store.enqueue_command('operator_round', number=rowid)
        self.store.mark_dispatched(cmd['id'], 'HOST')
        self.store.ack_command(cmd['id'], 'HOST', 'FAILED',
                               fail_reason='PROCESS_DIED')
        return item

    def test_drain_rearms_ready_item_with_no_live_command(self):
        self.seed_ready(age=1_000.0)
        rearmed = op_store.drain_obsolete_readys(self.store, stale_after=10.0,
                                                 now=2_000.0)
        self.assertEqual(rearmed, 1)
        # a fresh PENDING command now exists for the item
        self.assertEqual([c['state'] for c in self.store.pending_commands()],
                         ['PENDING'])

    def test_drain_skips_fresh_item(self):
        self.seed_ready(age=1_000.0)
        # item is too young relative to the gate — skipped
        rearmed = op_store.drain_obsolete_readys(self.store, stale_after=10.0,
                                                 now=1_005.0)
        self.assertEqual(rearmed, 0)

    def test_drain_skips_item_still_live(self):
        item, _ = op_store.put_signal(self.store, source='alert', kind='loop_stall',
                                      key='k1', payload={}, now=1_000.0)
        rowid = self.store.db.execute('SELECT rowid FROM work_items WHERE id=?',
                                      (item['id'],)).fetchone()['rowid']
        self.store.enqueue_command('operator_round', number=rowid)
        # a live PENDING command exists → not obsolete
        rearmed = op_store.drain_obsolete_readys(self.store, stale_after=10.0,
                                                 now=2_000.0)
        self.assertEqual(rearmed, 0)

    def test_floor_sweep_requeues_stale_claim(self):
        """Review BLOCKING-1: a command stuck CLAIMED (worker died mid-dispatch,
        never acked SPAWNED/FAILED) stays CLAIMED forever because the operator
        store is fed by the raw CommandStore (dispatch_operator lists PENDING
        without a sweep) — the drain's NOT EXISTS treats CLAIMED as live and
        the item's key stays muted. The floor sweep must requeue the stale
        claim (→PENDING, dispatchable again) before the drain runs."""
        item, _ = op_store.put_signal(self.store, source='alert', kind='loop_stall',
                                      key='k1', payload={}, now=1_000.0)
        rowid = self.store.db.execute('SELECT rowid FROM work_items WHERE id=?',
                                      (item['id'],)).fetchone()['rowid']
        cmd, _ = self.store.enqueue_command('operator_round', number=rowid)
        self.store.mark_dispatched(cmd['id'], 'HOST')  # claim never acked
        now = 9_000.0
        # put_item stamps created_at with wall-clock; force item + claim to
        # the past so the age/ack-window gates are deterministic.
        self.store.db.execute('UPDATE work_items SET created_at=? WHERE id=?',
                              (1_000.0, item['id']))
        self.store.db.execute("UPDATE commands SET claimed_at=1000.0, attempts=1 "
                              'WHERE id=?', (cmd['id'],))
        self.store.sweep_commands(now=now, ack_window=10, max_attempts=3)
        self.assertEqual(self.store.db.execute(
            'SELECT state FROM commands WHERE id=?', (cmd['id'],)).fetchone()['state'],
            'PENDING')
        # requeued PENDING is now a live command → drain defers to the dispatcher
        self.assertEqual(op_store.drain_obsolete_readys(
            self.store, stale_after=10.0, now=now), 0)

    def test_drain_rearms_after_claim_exhausted(self):
        """Reviewer BLOCKING-1 terminal half: once the stale claim has burned
        its dispatch attempts, the sweep FAILS it and the drain re-enqueues a
        fresh command — the READY item is no longer muted."""
        item, _ = op_store.put_signal(self.store, source='alert', kind='loop_stall',
                                      key='k1', payload={}, now=1_000.0)
        rowid = self.store.db.execute('SELECT rowid FROM work_items WHERE id=?',
                                      (item['id'],)).fetchone()['rowid']
        cmd, _ = self.store.enqueue_command('operator_round', number=rowid)
        self.store.mark_dispatched(cmd['id'], 'HOST')
        now = 9_000.0
        self.store.db.execute('UPDATE work_items SET created_at=? WHERE id=?',
                              (1_000.0, item['id']))
        self.store.db.execute("UPDATE commands SET claimed_at=1000.0, attempts=3 "
                              'WHERE id=?', (cmd['id'],))
        self.store.sweep_commands(now=now, ack_window=10, max_attempts=3)
        self.assertEqual(self.store.db.execute(
            'SELECT state FROM commands WHERE id=?', (cmd['id'],)).fetchone()['state'],
            'FAILED')
        rearmed = op_store.drain_obsolete_readys(self.store, stale_after=10.0,
                                                 now=now)
        self.assertEqual(rearmed, 1)


class WedgedRoundRecycleTests(unittest.TestCase):
    """2026-10-15 复盘缺陷：execution 已终态而 item 仍 RUNNING 的「楔死轮」
    （#302 修复前的 PROCESS_DIED 存量）把过期租约永远留在 leases 表，同 key
    新信号 claim 撞 UNIQUE(leases.resource) → 命令 FAILED（7 天 ~90 条）。
    recycle_after_fail 只达活轮、sweep_expired 只扫非终态 execution——楼层
    回收是唯一治愈路径。"""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='op-wedge-')
        self.store = op_store.open_store(self.home)
        item, _ = op_store.put_signal(self.store, source='alert', kind='loop_stall',
                                      key='k1', payload={'message': 'stall'},
                                      org_repo='mocx__skills', now=1_000.0)
        self.claim = face.claim(self.store, worker='HOST/operator', ttl=1_800,
                                item_id=item['id'])

    def tearDown(self):
        self.store.db.close()

    def _wedged(self, state='FAILED'):
        """复刻 10-08 存量形态：execution 被判终态，父 item 与租约原样遗留。"""
        self.store.db.execute("UPDATE executions SET state=? WHERE id=?",
                              (state, self.claim['id']))

    def test_recycle_wedged_round_frees_everything(self):
        self._wedged()
        receipt = op_store.recycle_wedged_rounds(self.store)
        self.assertEqual(receipt, {'items': 1, 'leases': 1})
        item = self.store.db.execute('SELECT state FROM work_items WHERE id=?',
                                     (self.claim['item_id'],)).fetchone()
        self.assertEqual(item['state'], 'CANCELLED')
        self.assertEqual(self.store.db.execute(
            'SELECT COUNT(*) FROM leases').fetchone()[0], 0)
        events = self.store.db.execute(
            "SELECT payload FROM events WHERE kind='operator.item_recycled'"
            " ORDER BY rowid DESC LIMIT 1").fetchone()
        payload = json.loads(events['payload'])
        self.assertEqual(payload['key'], 'op:alert:k1')
        self.assertEqual(payload['reason'], 'wedged_round_recycled')

    def test_recycle_removes_lock_dir(self):
        lock = self.store.lock_path('op:alert:k1') / 'agent.json'
        self.assertTrue(lock.exists(), 'claim 应留下文件锁')
        self._wedged()
        op_store.recycle_wedged_rounds(self.store)
        self.assertFalse(lock.exists())

    def test_recycled_key_claims_cleanly_again(self):
        self._wedged()
        op_store.recycle_wedged_rounds(self.store)
        fresh, created = op_store.put_signal(self.store, source='alert',
                                             kind='loop_stall', key='k1',
                                             payload={}, now=2_000.0)
        self.assertTrue(created)
        recla = face.claim(self.store, worker='HOST/operator', ttl=1_800,
                           item_id=fresh['id'])
        self.assertEqual(recla['item_id'], fresh['id'])

    def test_live_round_untouched(self):
        receipt = op_store.recycle_wedged_rounds(self.store)
        self.assertEqual(receipt, {'items': 0, 'leases': 0})
        item = self.store.db.execute('SELECT state FROM work_items WHERE id=?',
                                     (self.claim['item_id'],)).fetchone()
        self.assertEqual(item['state'], 'RUNNING')
        self.assertEqual(self.store.db.execute(
            'SELECT COUNT(*) FROM leases').fetchone()[0], 1)

    def test_orphan_lease_of_terminal_item_dropped(self):
        """纯孤儿租约（item 已终态但释放失败泄漏）同样回收——终态 execution
        的租约恒为泄漏，每个终结方都负责释放。"""
        self._wedged(state='SUCCEEDED')
        self.store.db.execute("UPDATE work_items SET state='SUCCEEDED' WHERE id=?",
                              (self.claim['item_id'],))
        receipt = op_store.recycle_wedged_rounds(self.store)
        self.assertEqual(receipt, {'items': 0, 'leases': 1})
        self.assertEqual(self.store.db.execute(
            'SELECT COUNT(*) FROM leases').fetchone()[0], 0)

    def test_unclaimed_running_item_not_touched(self):
        """无 execution 的 RUNNING item 不在回收口径内（不存在的形态宁可
        留给 drain/人工，不越权猜测）。"""
        self.store.db.execute("UPDATE work_items SET state='RUNNING' WHERE id=?",
                              (self.claim['item_id'],))
        self.store.db.execute('DELETE FROM leases')
        self.store.db.execute("DELETE FROM executions WHERE id=?",
                              (self.claim['id'],))
        receipt = op_store.recycle_wedged_rounds(self.store)
        self.assertEqual(receipt, {'items': 0, 'leases': 0})


class CollectCliSingleWriterTest(unittest.TestCase):
    """issue #302 defect 2: the watchdog cursor must have one writer. The
    serve consumer owns it; the collect CLI only ingests alerts. Running the
    CLI collect path must never touch ``ingest_watchdog`` (the old dual-writer
    raced the same dash meta cursor and replayed lines)."""

    def test_collect_cli_does_not_ingest_watchdog(self):
        """Operator section is forced present+enabled so the old dual-writer
        branch (had this fix regressed) WOULD have called ingest_watchdog from
        the CLI. Without a truthy config the operator store stays None and the
        branch is never reached — a vacuous pass."""
        import runpy
        script = _Path(__file__).resolve().parents[1] / 'scripts' / 'dashboard.py'
        fake_db = mock.MagicMock()
        fake_store = mock.MagicMock()
        config_path = _Path(tempfile.mkdtemp(prefix='op-cli-')) / '.code-loop'
        config_path.mkdir()
        (config_path / 'dashboard.json').write_text(
            json.dumps({'operator': {'enabled': True}}), encoding='utf-8')
        with mock.patch.object(_Path, 'home', return_value=config_path.parent), \
                mock.patch.object(_sys, 'argv',
                                  ['dashboard.py', 'collect', '--db', ':memory:']), \
                mock.patch.object(_sys, 'exit'), \
                mock.patch('codeloop.operator.ingest.ingest_watchdog') as ingest_wd, \
                mock.patch('codeloop.operator.ingest.ingest_alerts') as ingest_alerts, \
                mock.patch('codeloop.dash.collect.collect',
                           return_value={'failed': 0, 'projects': []}), \
                mock.patch('codeloop.dash.notify.push_new_alerts',
                           return_value={'error': None}), \
                mock.patch('codeloop.dash.store.connect', return_value=fake_db), \
                mock.patch('codeloop.operator.store.open_store', return_value=fake_store):
            runpy.run_path(str(script), run_name='__main__')
        ingest_wd.assert_not_called()


if __name__ == '__main__':
    unittest.main()