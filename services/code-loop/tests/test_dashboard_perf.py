#!/usr/bin/env python3
"""Dashboard read-path performance guards (project-switch latency fix).

The store had no secondary indexes at all: every per-PR verdict lookup
full-scanned comments (2270 such queries per overview+items+trends render,
~9.8s of pure SQL on the live store), and overview re-derived
pending_decisions / exceptions / loop_health twice per render because alerts
recomputed them internally. These tests pin the structural fixes: the query
indexes exist, the hot lookup plans use them, the loop_health rewrite stays
equivalent to the LEFT JOIN it replaces, and overview feeds each section to
alerts exactly once. New suites go in their own file (line-count ratchet,
issue #185 convention)."""
import unittest

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.dash import health, store
from _dashboard_store_base import StoreTestCase, gitea_issue, gitea_pr, ts

EXPECTED_INDEXES = (
    'idx_comments_scope',    # per-PR verdict lookups (the 2270x hotspot)
    'idx_issues_scope',      # per-project open-issue scans (board/todo/decisions)
    'idx_prs_scope',         # per-project open-PR scans (board/merges/funnel)
    'idx_exec_scope',        # per-project execution reads (runtime/board/health)
    'idx_wi_cover',          # covering index: loop_health last-activity scan
)

# The exact shape verdicts.latest_verdict_detail runs per PR; the plan must
# search the scope index, not SCAN the whole comments table.
PR_COMMENT_LOOKUP = ('SELECT poster, body, created_at FROM comments '
                     "WHERE project_id=? AND kind='pr' AND number=?")

# The join loop_health replaced (kept here as the behavioral reference).
OLD_LAST_ACTIVITY_SQL = (
    'SELECT wi.project_id, wi.kind, '
    'MAX(COALESCE(ex.created_at, wi.created_at)) AS last_created '
    'FROM work_items wi LEFT JOIN executions ex ON ex.project_id=wi.project_id '
    'AND ex.item_cp_id=wi.cp_id GROUP BY wi.project_id, wi.kind')


class IndexTests(StoreTestCase):
    def test_connect_creates_query_indexes(self):
        names = {r[0] for r in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        missing = [n for n in EXPECTED_INDEXES if n not in names]
        self.assertEqual([], missing)

    def test_pr_comment_lookup_uses_index_not_scan(self):
        plan = ' '.join(r[-1] for r in self.db.execute(
            'EXPLAIN QUERY PLAN ' + PR_COMMENT_LOOKUP, (1, 5)))
        self.assertIn('idx_comments_scope', plan)
        self.assertNotIn('SCAN comments', plan)


def _wi(cp_id, kind, created, state=None, key='k'):
    return {'id': cp_id, 'key': key, 'kind': kind, 'state': state,
            'priority': None, 'version': None, 'payload': '{}',
            'resources': None, 'created_at': created}


def _ex(cp_id, item_cp_id, created, state='SUCCEEDED'):
    # replace_runtime reads executions.item_id (the CP item id) — the same
    # key the kanban suite's execution() helper uses.
    return {'id': cp_id, 'item_id': item_cp_id, 'worker': 'w',
            'state': state, 'result': None, 'worktree': None, 'branch': None,
            'head': None, 'expires_at': None, 'created_at': created}


class LoopHealthTests(StoreTestCase):
    def _load(self, items, executions):
        pid = self.pid()
        store.upsert_project(self.db, 'a__b', 'cp.json', platform_url='http://g', repo='a/b')
        store.replace_runtime(self.db, pid, {'work_items': items,
                                             'executions': executions})
        return pid

    @staticmethod
    def _by_kind(rows, org):
        return {(r['kind'], r['last_activity'], r['running'])
                for r in rows if r['org_repo'] == org}

    def test_last_activity_matches_replaced_join(self):
        pid = self._load(
            [_wi(1, 'handler', ts(5)), _wi(2, 'reviewer', ts(6)),
             _wi(3, 'handler', ts(3))],
            [_ex(11, 1, ts(9, 1)), _ex(12, 1, ts(7))])
        reference = {}
        for row in self.db.execute(OLD_LAST_ACTIVITY_SQL):
            reference[(row['project_id'], row['kind'])] = row['last_created']
        rows = health.loop_health(self.db, now=self.now)
        got = {(pid, r['kind']): (r['last_activity'], r['running'])
               for r in rows if r['last_activity'] is not None}
        self.assertEqual(
            {k: (store.parse_ts(v), 0) for k, v in reference.items()}, got)

    def test_running_counts_survive(self):
        self._load([_wi(1, 'handler', ts(5), state='RUNNING'),
                    _wi(2, 'handler', ts(4), state='READY')],
                   [_ex(11, 1, ts(9, 1))])
        rows = health.loop_health(self.db, now=self.now)
        self.assertIn(('handler', store.parse_ts(ts(9, 1)), 1),
                      self._by_kind(rows, 'a__b'))

    def test_mixed_timestamp_formats_pick_newest_by_epoch(self):
        # work_items carries CP epoch text, executions ISO-8601 (both occur on
        # the live store). The old SQL compared the textual forms, so an older
        # ISO string outsorted a newer epoch one; the rewrite compares epochs.
        self._load([_wi(1, 'handler', '1789920000')],
                   [_ex(11, 1, ts(18))])   # 2026-09-18, older than the epoch
        rows = health.loop_health(self.db, now=self.now)
        self.assertIn(('handler', store.parse_ts('1789920000'), 0),
                      self._by_kind(rows, 'a__b'))


class AlertSharingTests(StoreTestCase):
    """overview must hand each derived section to alerts exactly once; alerts
    recomputing them internally is the double pass this fix removes. The
    names live in health's own namespace (``from .views import``), so patching
    health.* intercepts the internal calls."""

    def _loaded(self):
        pid = self.pid()
        store.apply_snapshot(self.db, pid, {'issues': [gitea_issue(1)],
                                            'prs': [gitea_pr(101)], 'base_sha': 'b'})
        return pid

    def test_overview_derives_each_section_once(self):
        self._loaded()
        calls = {}
        section_names = ('pending_decisions', '_exceptions', 'loop_health')
        originals = {name: getattr(health, name) for name in section_names}

        def counting(name, real):
            def inner(db, *a, **kw):
                calls[name] = calls.get(name, 0) + 1
                return real(db, *a, **kw)
            return inner
        try:
            for name, real in originals.items():
                setattr(health, name, counting(name, real))
            health.overview(self.db, now=self.now)
        finally:
            for name, real in originals.items():
                setattr(health, name, real)
        self.assertEqual({'pending_decisions': 1, '_exceptions': 1,
                          'loop_health': 1}, calls)

    def test_alerts_accept_precomputed_sections(self):
        self._loaded()
        section_names = ('pending_decisions', '_exceptions', 'loop_health')
        originals = {name: getattr(health, name) for name in section_names}

        def must_not_recompute(db, *a, **kw):
            raise AssertionError('alerts re-derived a section it was given')
        try:
            for name in section_names:
                setattr(health, name, must_not_recompute)
            found = health.alerts(self.db, now=self.now, decisions=[],
                                  exceptions=[], loops=[])
        finally:
            for name, real in originals.items():
                setattr(health, name, real)
        # The three injected sections contribute nothing on empty fixtures;
        # the derivation rules (collect staleness etc.) still run.
        self.assertIsInstance(found, list)


if __name__ == '__main__':
    unittest.main()
