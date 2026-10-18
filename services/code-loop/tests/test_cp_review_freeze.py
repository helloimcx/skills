#!/usr/bin/env python3
"""Reviewer freeze-deadlock regression tests (PR #232 incident, 2026-09-20).

A red verification signal must never outlive the round that produced it:
in-flight rounds cannot freeze themselves out of the derivation set, and a
cancelled round's evidence cannot freeze the PR forever. A same-version
CANCELLED item row must revive when sync re-derives its key, so every key in
the derivation set always has a claimable row. Real supersession (head/base
changed, PR closed) still fires the unchanged STALE_INPUT fencing.
"""
import json
import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from _service_test_base import ServiceTestsBase


def _red_payload(head, base_tip):
    """Verification evidence shaped like the gate's: a base-defect red at the
    live base tip — exactly the shape that froze review:232."""
    return {'head': head,
            'attribution': {'verdict': 'base-defect', 'base_ref': 'origin/main',
                            'base_tip_sha': base_tip, 'merge_base': 'm1',
                            'nodes': ['test_x'], 'runs': {}},
            'checks': [{'exit_code': 1, 'failure_class': 'code'}]}


class ReviewFreezeTests(ServiceTestsBase):
    def _open_pr(self):
        pr = self.pr()
        self.platform.data['prs'] = [pr]
        self.s.sync()
        return pr

    def _claim_review(self):
        ex = self.s.claim('reviewer', worker='worker-a')
        self.assertIsNotNone(ex)
        return ex

    def _live_review_items(self):
        return [i for i in self.s.store.items('reviewer') if i['state'] == 'READY']

    def test_inflight_signal_does_not_freeze(self):
        """A running round's own red must not drop its item from the
        derivation set (the 16:56:58 moment of the incident): the key stays
        in current_versions, so report's fencing check cannot misfire."""
        pr = self._open_pr()
        ex = self._claim_review()
        self.s.store.add_evidence(ex['id'], ex['token'], 'verification',
                                  _red_payload(pr['head']['sha'], self.platform.data['base_sha']))
        self.s.sync()
        current = self.s.store.db.execute("SELECT value FROM meta WHERE key='current_versions'").fetchone()
        self.assertIn('review:2', json.loads(current['value']),
                      'in-flight red froze the review item out of the derivation set')
        item = self.s.store.db.execute('SELECT state FROM work_items WHERE id=?',
                                       (ex['item_id'],)).fetchone()
        self.assertEqual(item['state'], 'RUNNING')

    def test_dead_round_signal_does_not_freeze(self):
        """A cancelled round's red must not freeze the PR: sync re-derives the
        key and the same-version CANCELLED row revives to READY."""
        pr = self._open_pr()
        ex = self._claim_review()
        self.s.store.add_evidence(ex['id'], ex['token'], 'verification',
                                  _red_payload(pr['head']['sha'], self.platform.data['base_sha']))
        db = self.s.store.db
        db.execute("UPDATE executions SET state='CANCELLED' WHERE id=?", (ex['id'],))
        db.execute("UPDATE work_items SET state='CANCELLED' WHERE id=?", (ex['item_id'],))
        # cancel_stale also clears the lease rows; the lock FILES stay behind
        # on disk so the re-claim below exercises the dead-owner takeover.
        db.execute("DELETE FROM leases WHERE execution_id=?", (ex['id'],))
        db.commit()
        self.s.sync()
        live = self._live_review_items()
        self.assertTrue(live, 'dead round red kept the review item frozen')
        self.assertEqual(live[0]['key'], 'review:2')
        # The revived item is claimable again: the dead round's lock files are
        # taken over (its execution row is terminal, so the owner is provably
        # dead), closing the full heal path.
        again = self.s.claim('reviewer', worker='worker-c')
        self.assertIsNotNone(again, 'revived item not claimable')
        self.assertEqual(again['item_id'], live[0]['id'])

    def test_incident_timeline_232(self):
        """Full counterfactual of the incident: mid-round red, the sync that
        froze review:232, then report/publish still lands the verdict."""
        pr = self._open_pr()
        ex = self._claim_review()
        self.s.store.add_evidence(ex['id'], ex['token'], 'verification',
                                  _red_payload(pr['head']['sha'], self.platform.data['base_sha']))
        self.s.sync()
        result = self.s.report(ex['id'], ex['token'], {
            'decision': 'Blocked', 'reviewer': 'reviewer-b',
            'summary': 'base red at tip', 'gate': {'signature': None, 'commands': [],
                                                   'note': 'base-defect at base tip'}})
        self.assertEqual(result['execution_state'], 'SUCCEEDED')
        item = self.s.store.db.execute('SELECT state FROM work_items WHERE id=?',
                                       (ex['item_id'],)).fetchone()
        self.assertEqual(item['state'], 'SUCCEEDED')

    def test_put_item_revives_cancelled_same_version(self):
        payload = {'task': 'PR_REVIEW'}
        row = self.s.store.put_item('review:9', 'reviewer', 'v1', payload, ['pr-9'])
        db = self.s.store.db
        db.execute("UPDATE work_items SET state='CANCELLED' WHERE id=?", (row['id'],))
        db.commit()
        again = self.s.store.put_item('review:9', 'reviewer', 'v1', payload, ['pr-9'])
        self.assertEqual(again['id'], row['id'])
        self.assertEqual(again['state'], 'READY')
        revived = db.execute("SELECT 1 FROM events WHERE kind='item.revived' AND payload LIKE ?",
                             ('%' + row['id'] + '%',)).fetchone()
        self.assertTrue(revived, 'item.revived event missing')

    def test_put_item_keeps_succeeded(self):
        payload = {'task': 'PR_REVIEW'}
        row = self.s.store.put_item('review:9', 'reviewer', 'v1', payload, ['pr-9'])
        db = self.s.store.db
        db.execute("UPDATE work_items SET state='SUCCEEDED' WHERE id=?", (row['id'],))
        db.commit()
        again = self.s.store.put_item('review:9', 'reviewer', 'v1', payload, ['pr-9'])
        self.assertEqual(again['id'], row['id'])
        self.assertEqual(again['state'], 'SUCCEEDED')

    def test_stale_still_fires_on_head_change(self):
        pr = self._open_pr()
        ex = self._claim_review()
        self.platform.data['prs'][0]['head']['sha'] = 'head2'
        self.expire_observer()
        self.s.sync()
        with self.assertRaises(ValueError) as ctx:
            self.s.report(ex['id'], ex['token'], {
                'decision': 'Blocked', 'reviewer': 'reviewer-b', 'summary': 'late',
                'gate': {'signature': None, 'commands': [], 'note': 'x'}})
        self.assertIn('STALE_INPUT', str(ctx.exception))

    def test_stale_still_fires_on_pr_closed(self):
        self._open_pr()
        ex = self._claim_review()
        self.platform.data['prs'][0]['state'] = 'closed'
        self.expire_observer()
        self.s.sync()
        with self.assertRaises(ValueError) as ctx:
            self.s.report(ex['id'], ex['token'], {
                'decision': 'Blocked', 'reviewer': 'reviewer-b', 'summary': 'late',
                'gate': {'signature': None, 'commands': [], 'note': 'x'}})
        self.assertIn('STALE_INPUT', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
