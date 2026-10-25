"""Operator ingest gate: whitelist, cooldown, daily cap, self-exclusion,
freshness, decision log (spec docs/specs/2026-09-30-cp-operator-agent.md)."""
import json
import sys as _sys
import tempfile
import unittest
from pathlib import Path

for _p in (str(Path(__file__).resolve().parents[1]),
           str(Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.operator import config as op_config
from codeloop.operator import ingest as op_ingest
from codeloop.operator import store as op_store


def cfg(**over):
    base = op_config.load_operator_config({'operator': dict(enabled=True, **over)})
    return base


def alert(rule='loop_stall', org='mocx__skills', key='k1', message='stall',
          severity='high'):
    return {'severity': severity, 'rule': rule, 'org_repo': org, 'key': key,
            'message': message}


class ConfigTests(unittest.TestCase):
    def test_missing_section_is_none(self):
        self.assertIsNone(op_config.load_operator_config({}))
        self.assertIsNone(op_config.load_operator_config({'notify': {}}))

    def test_defaults_and_unknown_key_rejected(self):
        c = cfg()
        self.assertTrue(c['enabled'])
        self.assertEqual(c['cooldown_minutes'], 30)
        self.assertEqual(c['max_rounds_per_day'], 12)
        self.assertFalse(c['watchdog_auto_spawn'])
        self.assertIn('loop_stall', c['alert_rules'])
        self.assertNotIn('needs_human_aging', c['alert_rules'])
        self.assertIn('worker.task.succeeded', c['event_kinds'])
        with self.assertRaises(ValueError):
            op_config.load_operator_config({'operator': {'nope': 1}})

    def test_agent_validated_when_given(self):
        with self.assertRaises(ValueError):
            cfg(agent='not-a-runner')
        c = cfg(agent='claude', model=None)
        self.assertEqual(c['agent'], 'claude')


class IngestAlertTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='op-ingest-')
        self.store = op_store.open_store(self.home)

    def tearDown(self):
        self.store.db.close()

    def decisions(self, alerts, **over):
        return op_ingest.ingest_alerts(self.store, cfg(**over), alerts, home=self.home)

    def test_fires_signal_item_and_logs(self):
        ds = self.decisions([alert()])
        self.assertEqual(len(ds), 1)
        self.assertEqual(ds[0]['decision'], 'fired')
        self.assertIsNotNone(op_store.active_signal(self.store, 'alert', 'k1'))
        log = json.loads((Path(self.home) / 'logs' / 'operator-log.jsonl').read_text(
            encoding='utf-8').splitlines()[-1])
        self.assertEqual(log['decision'], 'fired')
        self.assertEqual(log['source'], 'alert')

    def test_non_whitelisted_rule_skipped(self):
        ds = self.decisions([alert(rule='needs_human_aging')])
        self.assertEqual(ds[0]['decision'], 'skipped_rule')
        self.assertIsNone(op_store.active_signal(self.store, 'alert', 'k1'))

    def test_cooldown_window_blocks_same_key_only(self):
        now = 10_000.0
        ds = op_ingest.ingest_alerts(self.store, cfg(), [alert()], now=now,
                                     home=self.home)
        self.assertEqual(ds[0]['decision'], 'fired')
        ds = op_ingest.ingest_alerts(self.store, cfg(), [alert()], now=now + 60,
                                     home=self.home)
        self.assertEqual(ds[0]['decision'], 'skipped_cooldown')
        # a different key is not blocked by the first key's cooldown
        ds = op_ingest.ingest_alerts(self.store, cfg(), [alert(key='k2')],
                                     now=now + 60, home=self.home)
        self.assertEqual(ds[0]['decision'], 'fired')

    def test_daily_cap_degrades_to_webhook_only(self):
        now = 10_000.0
        ds = op_ingest.ingest_alerts(self.store, cfg(max_rounds_per_day=1),
                                     [alert(key='k1')], now=now, home=self.home)
        self.assertEqual(ds[0]['decision'], 'fired')
        ds = op_ingest.ingest_alerts(self.store, cfg(max_rounds_per_day=1),
                                     [alert(key='k2')], now=now + 1, home=self.home)
        self.assertEqual(ds[0]['decision'], 'skipped_cap')
        self.assertTrue(ds[0]['degrade_webhook'])

    def test_stale_signal_not_fired(self):
        ds = op_ingest.ingest_alerts(self.store, cfg(), [alert()], now=10_000.0,
                                     fresh=False, home=self.home)
        self.assertEqual(ds[0]['decision'], 'stale_noop')

    def test_active_signal_dedup(self):
        op_ingest.ingest_alerts(self.store, cfg(), [alert()], now=10_000.0,
                                home=self.home)
        # past the cooldown but the previous item is still active (no round
        # picked it up yet) — a second fire would just supersede it, so skip
        self.store.db.execute(
            "UPDATE meta SET value=? WHERE key LIKE 'operator_last:%'", ('0',))
        ds = op_ingest.ingest_alerts(self.store, cfg(), [alert()], now=10_000.0,
                                     home=self.home)
        self.assertEqual(ds[0]['decision'], 'skipped_active')


class IngestEventTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='op-ev-')
        self.store = op_store.open_store(self.home)

    def tearDown(self):
        self.store.db.close()

    def test_whitelisted_event_fires(self):
        ds = op_ingest.ingest_event(self.store, cfg(), 'sync.failed',
                                    {'detail': 'boom'}, project='mocx__skills',
                                    now=1_000.0, home=self.home)
        self.assertEqual(ds['decision'], 'fired')
        self.assertEqual(ds['item']['key'], 'op:cp_event:sync.failed|mocx__skills|')

    def test_exam_prefix_and_unknown_kind(self):
        ds = op_ingest.ingest_event(self.store, cfg(), 'exam.invalid', {},
                                    project='p', now=1_000.0, home=self.home)
        self.assertEqual(ds['decision'], 'fired')
        ds = op_ingest.ingest_event(self.store, cfg(), 'publication.sent', {},
                                    project='p', now=1_001.0, home=self.home)
        self.assertEqual(ds['decision'], 'skipped_rule')

    def test_operator_events_never_self_consumed(self):
        ds = op_ingest.ingest_event(self.store, cfg(), 'operator.act', {},
                                    project='', now=1_000.0, home=self.home)
        self.assertEqual(ds['decision'], 'skipped_self')
        # project-CP bookkeeping kinds are simply not watched
        ds = op_ingest.ingest_event(self.store, cfg(), 'command.enqueued', {},
                                    project='', now=1_000.0, home=self.home)
        self.assertEqual(ds['decision'], 'skipped_rule')

    def test_pr_repair_event_becomes_machine_task_without_operator_round(self):
        class Dispatcher:
            def __init__(self):
                self.calls = []

            def enqueue_worker_task(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return {'created': True, 'task': {'id': 'task-1'}, 'dispatched': 0}

        dispatcher = Dispatcher()
        event = {'kind': 'pr.handler_repair_needed', 'entity': 'pr:42',
                 'source_event_id': 'pr-repair:source-1', 'number': 42,
                 'reasons': ['changes_requested', 'merge_conflict'],
                 'source_ids': ['review:1', 'merge:1']}
        result = op_ingest.ingest_event(
            self.store, cfg(), 'pr.handler_repair_needed', event,
            project='mocx__skills', event_id=91, dispatcher=dispatcher,
            now=1_000.0, home=self.home)
        self.assertEqual('fired', result['decision'])
        self.assertEqual('task-1', result['task']['id'])
        args, kwargs = dispatcher.calls[0]
        self.assertEqual('pr.repair', args[0])
        self.assertEqual('mocx__skills', kwargs['project'])
        self.assertEqual(42, kwargs['payload']['number'])
        self.assertEqual(['changes_requested', 'merge_conflict'],
                         kwargs['payload']['reasons'])
        self.assertEqual('pr-repair:source-1', kwargs['source_event_id'])

    def test_worker_stall_event_carries_machine_and_allowlisted_actions(self):
        class Dispatcher:
            def __init__(self):
                self.calls = []

            def enqueue_worker_task(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return {'created': True, 'task': {'id': 'diag-1'}, 'dispatched': 1}

        dispatcher = Dispatcher()
        event = {'kind': 'worker.loop_stalled', 'entity': 'worker:m1:discover',
                 'source_event_id': 'worker-stall:event-1', 'machine_id': 'm1',
                 'loop': 'discover', 'last_execution_id': 'exec-7',
                 'age_seconds': 70000}
        result = op_ingest.ingest_event(
            self.store, cfg(), 'worker.loop_stalled', event,
            project='mocx__skills', event_id=92, dispatcher=dispatcher,
            now=1_000.0, home=self.home)
        self.assertEqual('diag-1', result['task']['id'])
        args, kwargs = dispatcher.calls[0]
        self.assertEqual('worker.project_diagnose', args[0])
        self.assertEqual('m1', kwargs['target_machine_id'])
        self.assertEqual('mocx__skills', kwargs['project'])
        self.assertIn('discover.run_once', kwargs['allowed_actions'])

    def test_worker_task_result_is_logged_without_starting_an_operator_round(self):
        result = op_ingest.ingest_event(
            self.store, cfg(), 'worker.task.succeeded',
            {'task': 'task-1', 'task_type': 'worker.project_diagnose',
             'state': 'SUCCEEDED', 'machine_id': 'm1',
             'result': {'summary': 'discover is healthy'}},
            now=2000, home=self.home)
        self.assertEqual('task_result', result['decision'])
        self.assertEqual(0, self.store.db.execute(
            "SELECT count(*) FROM work_items WHERE kind='operator'").fetchone()[0])

    def test_watchdog_ingestion_with_line_cursor(self):
        log = Path(self.home) / 'watchdog.jsonl'
        log.write_text(json.dumps({'ts': 1.0, 'action': 'spawn',
                                   'task': 'code-loop-cp-serve'}) + '\n'
                       + json.dumps({'ts': 2.0, 'action': 'probe-ok',
                                     'task': 'x'}) + '\n', encoding='utf-8')
        ds = op_ingest.ingest_watchdog(self.store, cfg(), log, db=self.store.db,
                                       now=10.0, home=self.home)
        self.assertEqual([d['decision'] for d in ds], ['fired'])
        ds = op_ingest.ingest_watchdog(self.store, cfg(), log, db=self.store.db,
                                       now=20.0, home=self.home)
        self.assertEqual(ds, [])

    def test_watchdog_cursor_advances_past_non_spawn_rows(self):
        """(review MINOR-1) The line cursor must move on every parseable row,
        not only on whitelisted actions — otherwise watchdog.jsonl rows after
        a non-spawn line (probe-ok, stale, ...) are re-read on every pass."""
        log = Path(self.home) / 'watchdog.jsonl'
        log.write_text(json.dumps({'ts': 1.0, 'action': 'probe-ok',
                                   'task': 'x'}) + '\n', encoding='utf-8')
        op_ingest.ingest_watchdog(self.store, cfg(), log, db=self.store.db,
                                  now=10.0, home=self.home)
        # a fresh spawn line is appended *after* a non-spawn line: the cursor
        # must consume the probe first, then fire only the spawn
        cursor = self.store.db.execute(
            "SELECT value FROM meta WHERE key='operator_watchdog_line'").fetchone()
        self.assertEqual(cursor['value'], '1')
        with log.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps({'ts': 2.0, 'action': 'spawn',
                                 'task': 'code-loop-cp-serve'}) + '\n')
        ds = op_ingest.ingest_watchdog(self.store, cfg(), log, db=self.store.db,
                                       now=20.0, home=self.home)
        self.assertEqual([d['decision'] for d in ds], ['fired'])
        self.assertEqual(self.store.db.execute(
            "SELECT value FROM meta WHERE key='operator_watchdog_line'"
        ).fetchone()['value'], '2')


class NotifyHookTests(unittest.TestCase):
    def test_push_new_alerts_invokes_on_alerts(self):
        from unittest import mock
        from codeloop.dash import notify
        import sqlite3
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        captured = []
        fake_alert = alert()
        with mock.patch.object(notify.store, 'alerts', return_value=[fake_alert]), \
             mock.patch.object(notify.store, 'get_meta', return_value='[]'), \
             mock.patch.object(notify.store, 'set_meta'):
            note = notify.push_new_alerts(db, {}, on_alerts=captured.append)
        self.assertEqual(captured, [[fake_alert]])
        self.assertIsNone(note['error'])

    def test_on_alerts_failure_is_isolated(self):
        from unittest import mock
        from codeloop.dash import notify
        import sqlite3
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        with mock.patch.object(notify.store, 'alerts', return_value=[alert()]), \
             mock.patch.object(notify.store, 'get_meta', return_value='[]'), \
             mock.patch.object(notify.store, 'set_meta'):
            note = notify.push_new_alerts(
                db, {}, on_alerts=lambda a: (_ for _ in ()).throw(RuntimeError('boom')))
        self.assertIn('operator ingest failed', note['error'])


if __name__ == '__main__':
    unittest.main()
