#!/usr/bin/env python3
import copy
import json
import os
import socket
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts"),
           str(_Path(__file__).resolve().parent)):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.cp.runtime import EXAM_PATH
from codeloop.cp.exam import GUARD_NOTE as EXAM_GUARD_NOTE
from codeloop.cp.service import Service
from _service_test_base import Platform, git, ServiceTestsBase

class ServiceTests(ServiceTestsBase):
    def test_claim_requires_machine_enable_marker(self):
        self.s.sync()
        self.assertEqual(len(self.s.store.items('discover')), 1)
        self.s.disable()
        with self.assertRaisesRegex(ValueError, 'MACHINE_NOT_ENABLED'):
            self.s.claim('discover', 'worker')
        with self.assertRaisesRegex(ValueError, 'MACHINE_NOT_ENABLED'):
            self.s.report('missing', 1, {'decision': 'CONFIRMED'})
        # The retired verbs are gone from the coordinator surface entirely.
        for verb in ('finish', 'heartbeat', 'recover'):
            self.assertFalse(hasattr(self.s, verb), verb)
        self.s.enable()
        execution = self.s.claim('discover', 'worker')
        self.assertEqual(execution['item_id'], self.s.store.items('discover')[0]['id'])

    def test_claim_without_worker_derives_host_and_loop(self):
        """--worker is optional in-process too: the local carrier records
        <hostname>/<loop>, so sessions stop inventing their own labels
        (which drifted and once even claimed a reviewer item as "handler")."""
        self.s.sync()
        execution = self.s.claim('discover', None)
        self.assertEqual(socket.gethostname().strip() + '/discover', execution['worker'])

    def test_claim_execution_carries_no_report_result_field(self):
        """The claim response must not carry the executions-table `result`
        column: it is definitionally null at claim time and its presence let
        a loop session read 「result: null」 off a claimed item and treat the
        round as empty — 14 claimed PR reviews dropped in a row
        (ai_quality_analysis, 2026-10-08/09). Both carriers (in-process
        claim_raw and the server route) sit on this return."""
        self.s.sync()
        execution = self.s.claim('discover', 'worker')
        self.assertIsNotNone(execution)
        self.assertIn('item_id', execution)
        self.assertNotIn('result', execution)
        # claim_raw shares the shape for the in-process CLI carrier.
        self.s.store.put_item('pr-1', 'reviewer', 'v1', {'pr': 1}, ['pr-1'])
        envelope = self.s.claim_raw('reviewer', 'worker2')
        self.assertNotIn('result', envelope['execution'])

    def test_blank_worker_is_treated_as_omitted(self):
        self.s.sync()
        execution = self.s.claim('discover', '   ')
        self.assertEqual(socket.gethostname().strip() + '/discover', execution['worker'])

    def test_claim_collapses_silent_corpse_in_same_call(self):
        """A ghost round (died right after claim) must not wedge the queue
        until its whole lease lapses: the claim that finds it collapses it
        first and hands the item to this very round (ai_vector, 2026-09-30)."""
        self.s.sync()
        self.s.cfg['orphan_idle_seconds'] = 7200
        ghost = self.s.claim('discover', 'ghost')
        self.assertIsNotNone(ghost)
        self.s.store.db.execute(
            'UPDATE executions SET created_at=?, last_seen_at=NULL WHERE id=?',
            (time.time() - 9999, ghost['id']))
        execution = self.s.claim('discover', 'worker')
        self.assertIsNotNone(execution)
        self.assertEqual(ghost['item_id'], execution['item_id'])
        self.assertEqual('CANCELLED', self.s.store.db.execute(
            'SELECT state FROM executions WHERE id=?', (ghost['id'],)).fetchone()[0])

    def test_claim_without_collapse_config_keeps_legacy_behavior(self):
        """Hand-built configs without the key (older test fixtures, embedded
        consumers) must not change behavior: no key, no collapse."""
        self.s.sync()
        ghost = self.s.claim('discover', 'ghost')
        self.assertIsNotNone(ghost)
        self.s.store.db.execute(
            'UPDATE executions SET created_at=?, last_seen_at=NULL WHERE id=?',
            (time.time() - 9999, ghost['id']))
        self.assertNotIn('orphan_idle_seconds', self.s.cfg)
        self.assertIsNone(self.s.claim('discover', 'worker'))
        self.assertEqual('RUNNING', self.s.store.db.execute(
            'SELECT state FROM executions WHERE id=?', (ghost['id'],)).fetchone()[0])

    def test_failure_keeps_snapshot_but_blocks_claim(self):
        self.s.sync()
        before = self.s.snapshot()
        self.platform.fail = True
        self.expire_observer()
        with self.assertRaises(ValueError):
            self.s.sync()
        self.assertEqual(self.s.snapshot(), before)
        with self.assertRaises(ValueError):
            self.s.claim('discover', 'worker')

    def test_snapshot_fetch_runs_outside_write_transaction(self):
        observed = []
        original = self.platform.snapshot

        def spy(known=None):
            observed.append(self.s.store.db.in_transaction)
            return original(known)

        self.platform.snapshot = spy
        self.s.sync()
        # A slow remote fetch must not hold BEGIN IMMEDIATE and block claims.
        self.assertEqual([False], observed)

    def test_sync_serves_the_observer_store_without_recrawling(self):
        # The shared observer store is the single crawl path: within its TTL a
        # second sync must not touch the platform, and the facts it derives
        # from are exactly the crawled ones (no work-item version drift).
        self.platform.data['issues'] = [dict(self.issue(), updated_at='t1')]
        self.platform.data['prs'] = [dict(self.pr(), updated_at='t1')]
        self.s.sync()
        self.assertEqual([{}], self.platform.known_received)
        version = self.s.store.items('reviewer')[0]['version']
        self.s.sync()
        self.assertEqual(1, len(self.platform.known_received), 'cache hit must not crawl')
        self.assertEqual(version, self.s.store.items('reviewer')[0]['version'])
        # Once the store ages out, the crawl resumes and receives the
        # incremental basis the store kept.
        self.expire_observer()
        self.s.sync()
        self.assertEqual({('issue', 1): ('open', 't1'), ('pr', 2): ('open', 't1')},
                         self.platform.known_received[1])

    def test_sync_falls_back_to_a_live_crawl_without_a_stored_snapshot(self):
        # First observation on a machine: the store is empty, so the sync
        # crawls once itself instead of failing closed.
        self.platform.data['issues'] = [self.issue()]
        self.s.sync()
        self.assertEqual(1, len(self.platform.known_received))
        self.assertEqual(1, len([x for x in self.s.store.items('handler')
                                 if x['state'] == 'READY']))

    def test_source_versions_and_issue_filter(self):
        self.platform.data['issues'] = [self.issue(), dict(self.issue(), number=3, title='[Gap] product')]
        self.platform.data['prs'] = [self.pr()]
        self.s.sync()
        old = self.s.store.items('reviewer')[0]['version']
        self.assertEqual(len([x for x in self.s.store.items('handler') if x['state']=='READY']), 0)
        self.platform.data['issues'][0]['comments'].append({'body': 'new evidence'})
        self.expire_observer()
        self.s.sync()
        current = [x for x in self.s.store.items('reviewer') if x['state']=='READY']
        self.assertEqual(len(current), 1)
        self.assertNotEqual(current[0]['version'], old)

    def test_claim_reclaims_expired_zombie_in_the_same_round(self):
        self.platform.data['issues'] = [self.issue()]
        self.s.sync()
        first = self.s.claim('discover', 'w1')
        self.assertIsNotNone(first)
        past = time.time() - 2 * self.cfg['lease_seconds']
        self.s.store.db.execute('UPDATE executions SET expires_at=? WHERE id=?', (past, first['id']))
        self.s.store.db.execute('UPDATE leases SET expires_at=? WHERE execution_id=?',
                                (past, first['id']))
        # Same round, same input version: the sweep reclaims the zombie (no
        # result, clean outbox -> CANCELLED, item re-READY) and the claim
        # proceeds immediately — no next round, no BLOCKED tombstone, no
        # operator.
        second = self.s.claim('discover', 'w2')
        self.assertIsNotNone(second)
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(first['item_id'], second['item_id'])
        state = self.s.store.db.execute('SELECT state FROM executions WHERE id=?',
                                        (first['id'],)).fetchone()['state']
        self.assertEqual('CANCELLED', state)
        kinds = [r['kind'] for r in self.s.store.db.execute(
            "SELECT kind FROM events WHERE kind IN ('execution.swept','execution.stolen')")]
        self.assertEqual(['execution.swept'], kinds)

    def test_report_without_action_derives_terminal(self):
        s, p = self._fresh({})
        p.data['issues'] = [self.issue()]
        s.sync()
        e = s.claim('discover', 'worker')
        result = s.report(e['id'], e['token'],
                          {'decision': 'REJECTED', 'reviewer': 'verifier', 'summary': 'not a bug'})
        self.assertEqual(result['execution_state'], 'SUCCEEDED')
        state = s.store.db.execute('SELECT state FROM executions WHERE id=?',
                                   (e['id'],)).fetchone()['state']
        self.assertEqual('SUCCEEDED', state)
        self.assertEqual(0, s.store.db.execute('SELECT COUNT(*) FROM leases').fetchone()[0])

    def test_stale_input_cancels_execution_automatically(self):
        s, p = self._fresh({})
        p.data['issues'] = [self.issue()]
        s.sync()
        e = s.claim('discover', 'worker')
        # The platform advanced; the next sync mints a new READY version while
        # the claimed row stays RUNNING on the superseded input.
        p.data['base_sha'] = 'base2'
        self.expire_observer()
        s.sync()
        with self.assertRaisesRegex(ValueError, 'STALE_INPUT') as cm:
            s.report(e['id'], e['token'],
                     {'decision': 'CONFIRMED', 'reviewer': 'verifier', 'summary': 'stale round'})
        self.assertIn('reclaim', str(cm.exception))
        state = s.store.db.execute('SELECT state FROM executions WHERE id=?',
                                   (e['id'],)).fetchone()['state']
        self.assertEqual('CANCELLED', state)
        self.assertEqual(0, s.store.db.execute('SELECT COUNT(*) FROM leases').fetchone()[0])
        # The fresh version is claimable right away: the agent just re-claims.
        again = s.claim('discover', 'worker2')
        self.assertIsNotNone(again)
        self.assertNotEqual(again['item_id'], e['item_id'])

    def test_stale_input_with_unresolved_publication_stays_fenced(self):
        s, p = self._fresh({})
        p.data['issues'] = [self.issue()]
        s.sync()
        e = s.claim('discover', 'worker')
        s.store.db.execute(
            "INSERT INTO outbox VALUES ('op', ?, 'create_issue', '{}', 'SENDING', NULL)", (e['id'],))
        p.data['base_sha'] = 'base2'
        self.expire_observer()
        s.sync()
        with self.assertRaisesRegex(ValueError, 'reconcile'):
            s.report(e['id'], e['token'],
                     {'decision': 'CONFIRMED', 'reviewer': 'verifier', 'summary': 'x'})
        state = s.store.db.execute('SELECT state FROM executions WHERE id=?',
                                   (e['id'],)).fetchone()['state']
        self.assertEqual('RUNNING', state)

    def test_claim_refreshes_stale_snapshot_inline(self):
        s, p = self._fresh({})
        s.sync()
        # The platform grew after the last sync and the snapshot went stale.
        p.data['issues'] = [self.issue()]
        s.set_meta('synced_at', time.time() - 400)
        self.expire_observer()
        # Old protocol: SNAPSHOT_UNKNOWN_OR_STALE; now claim refreshes inline.
        e = s.claim('discover', 'worker')
        self.assertIsNotNone(e)

    def test_claim_unknown_item_selector_is_loud(self):
        s, p = self._fresh({})
        p.data['issues'] = [self.issue()]
        s.sync()
        with self.assertRaisesRegex(ValueError, 'ITEM_UNKNOWN'):
            s.claim('handler', 'worker', item_id='issue:404')
        # An 8-char prefix of a real id must never match.
        item = s.store.items('discover')[0]
        with self.assertRaisesRegex(ValueError, 'ITEM_UNKNOWN'):
            s.claim('discover', 'worker', item_id=item['id'][:8])

    def test_claim_sweep_converges_legacy_database_states(self):
        # Simulated legacy DB (pre-derivation rows): a reported+SENT zombie and
        # a BLOCKED tombstone from the old auto-recover. One claim converges
        # the zombie and leaves the tombstone readable -- no migration script.
        s, p = self._fresh({})
        p.data['issues'] = [self.issue(),
                            dict(self.issue(), number=2, title='[Content] second')]
        s.sync()
        zombie = s.claim('handler', 'old')
        result = {'decision': 'APPROVE', 'reviewer': 'verifier', 'summary': 'done',
                  'action': 'close_issue', 'payload': {'number': 1}}
        s.store.db.execute('UPDATE executions SET result=? WHERE id=?',
                           (json.dumps(result), zombie['id']))
        from codeloop.cp.runtime import version as _version
        op = _version([zombie['id'], result], {})
        s.store.db.execute(
            "INSERT INTO outbox VALUES (?, ?, 'close_issue', '{}', 'SENT', '{}')", (op, zombie['id']))
        past = time.time() - 7200
        s.store.db.execute('UPDATE executions SET expires_at=? WHERE id=?', (past, zombie['id']))
        s.store.put_item('issue:legacy', 'handler', 'v1', {'task': 'x'}, ['issue-99'],
                         state='BLOCKED')
        # New code claims through the same entry point: sweep converges the
        # zombie to SUCCEEDED (facts prove completion), tombstone untouched.
        fresh = s.claim('handler', 'new')
        self.assertIsNotNone(fresh)
        states = {r['id']: r['state'] for r in s.store.db.execute(
            'SELECT id, state FROM executions').fetchall()}
        self.assertEqual('SUCCEEDED', states[zombie['id']])
        self.assertEqual('RUNNING', states[fresh['id']])
        self.assertEqual('BLOCKED', [x for x in s.store.items() if x['key'] == 'issue:legacy'][0]['state'])

    def test_write_touchpoints_renew_the_lease(self):
        s, p = self._fresh({})
        p.data['issues'] = [self.issue()]
        s.sync()
        e = s.claim('discover', 'worker')
        past = time.time() - 7200
        s.store.db.execute('UPDATE executions SET expires_at=? WHERE id=?', (past, e['id']))
        # report is a touchpoint: it renews the lease as a side effect.
        s.report(e['id'], e['token'],
                 {'decision': 'REJECTED', 'reviewer': 'verifier', 'summary': 'no bug'})
        row = s.store.db.execute('SELECT expires_at FROM executions WHERE id=?',
                                 (e['id'],)).fetchone()
        self.assertGreater(row['expires_at'], time.time())

    def test_claim_ttl_override_bounds_the_handoff_window(self):
        """The dashboard's pre-claim asks for a short lease: the claim lands at
        click time, the session's bind renews it to the full lease, and a claim
        nobody adopts stops blocking the item after the window."""
        s, p = self._fresh({})
        s.sync()
        e = s.claim('discover', 'host/reviewer', ttl=300)
        left = s.store.db.execute('SELECT expires_at FROM executions WHERE id=?',
                                  (e['id'],)).fetchone()['expires_at'] - time.time()
        self.assertGreater(left, 0)
        self.assertLessEqual(left, 300)

    def test_claim_default_ttl_is_the_configured_lease(self):
        s, p = self._fresh({})
        s.sync()
        e = s.claim('discover', 'worker')
        left = s.store.db.execute('SELECT expires_at FROM executions WHERE id=?',
                                  (e['id'],)).fetchone()['expires_at'] - time.time()
        self.assertGreater(left, self.cfg['lease_seconds'] - 60)

    def test_abandon_ends_the_lease_and_requeues_immediately(self):
        """Holder-side abandonment: the round never started, so the standard
        sweep derivation closes it now — CANCELLED + item back to READY + locks
        released — instead of after the full lease. The next claim (no waiting,
        no operator) picks the very same item up."""
        s, p = self._fresh({})
        s.sync()
        e = s.claim('discover', 'host/reviewer', ttl=300)
        self.assertTrue((s.store.locks_dir / 'issue-discover').is_dir())
        s.abandon(e['id'], e['token'])
        self.assertEqual('CANCELLED', s.store.db.execute(
            'SELECT state FROM executions WHERE id=?', (e['id'],)).fetchone()['state'])
        self.assertEqual('READY', s.store.db.execute(
            'SELECT state FROM work_items WHERE id=?', (e['item_id'],)).fetchone()['state'])
        self.assertEqual(0, s.store.db.execute('SELECT COUNT(*) FROM leases').fetchone()[0])
        self.assertFalse((s.store.locks_dir / 'issue-discover').is_dir())
        again = s.claim('discover', 'worker2')
        self.assertIsNotNone(again)
        self.assertEqual(e['item_id'], again['item_id'])

    def test_abandon_refuses_foreign_token_and_pending_publication(self):
        s, p = self._fresh({})
        s.sync()
        e = s.claim('discover', 'host/reviewer')
        with self.assertRaisesRegex(ValueError, 'fenced'):
            s.abandon(e['id'], e['token'] + 1)
        s.store.db.execute("INSERT INTO outbox VALUES ('op', ?, 'create_issue', '{}', "
                           "'SENDING', NULL)", (e['id'],))
        with self.assertRaisesRegex(ValueError, 'UNFINISHED_PUBLICATION'):
            s.abandon(e['id'], e['token'])
        # A possibly-successful external write is never auto-declared failed:
        # the execution stays active for operator reconcile.
        self.assertEqual('RUNNING', s.store.db.execute(
            'SELECT state FROM executions WHERE id=?', (e['id'],)).fetchone()['state'])


if __name__ == '__main__':
    unittest.main()
