#!/usr/bin/env python3
"""Observer tests: single crawl path, raw-JSON equivalence, driver isolation."""
import copy
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.cp import observer


def issue(n, state='open', updated='2026-09-01T00:00:00Z', comments=None):
    return {'number': n, 'state': state, 'title': 'issue %d' % n, 'body': 'b',
            'labels': [], 'updated_at': updated, 'comments': comments or []}


def pr(n, state='open', updated='2026-09-02T00:00:00Z'):
    return {'number': n, 'state': state, 'title': 'pr %d' % n, 'body': 'Closes #1',
            'updated_at': updated, 'merged': False,
            'head': {'sha': 's%d' % n, 'ref': 'feature/x'},
            'base': {'sha': 'base1', 'ref': 'main'},
            'comments': [], 'reviews': [], 'checks': []}


def snapshot_of():
    return {'base_sha': 'base1', 'issues': [issue(1), issue(2, state='closed')],
            'prs': [pr(3)]}


def crawl_of(data, seen=None):
    def crawl(known=None):
        if seen is not None:
            seen.append(copy.deepcopy(known))
        return copy.deepcopy(data)
    return crawl


def failing_crawl(exc):
    def crawl(known=None):
        raise exc
    return crawl


class ObserverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'observer.sqlite')

    def tearDown(self):
        self.tmp.cleanup()

    def test_refresh_stores_and_assembles_equivalently(self):
        # Within the TTL the stored snapshot is served without a crawl, and
        # the reassembled object equals the crawled facts (order included):
        # versions derived from it can never drift.
        data = snapshot_of()
        seen = []
        observer.refresh(self.db, 'a__b', crawl_of(data, seen))
        self.assertEqual([{}], seen)
        again = observer.snapshot(self.db, 'a__b', 300, crawl_of({'base_sha': 'other'}))
        self.assertEqual(data, again)

    def test_refresh_passes_stored_known_to_the_crawl(self):
        seen = []
        observer.refresh(self.db, 'a__b', crawl_of(snapshot_of(), seen))
        observer.refresh(self.db, 'a__b', crawl_of(snapshot_of(), seen))
        self.assertEqual({('issue', 1): ('open', '2026-09-01T00:00:00Z'),
                          ('issue', 2): ('closed', '2026-09-01T00:00:00Z'),
                          ('pr', 3): ('open', '2026-09-02T00:00:00Z')}, seen[1])

    def test_snapshot_crawls_once_per_ttl_window(self):
        seen = []
        crawl = crawl_of(snapshot_of(), seen)
        observer.snapshot(self.db, 'a__b', 300, crawl)
        observer.snapshot(self.db, 'a__b', 300, crawl)
        self.assertEqual(1, len(seen))
        # An expired store refreshes from the same crawl callback. (A negative
        # TTL keeps this deterministic on coarse Windows clocks.)
        observer.snapshot(self.db, 'a__b', -1, crawl)
        self.assertEqual(2, len(seen))

    def test_projects_are_isolated_in_one_store(self):
        observer.refresh(self.db, 'a__b', crawl_of(snapshot_of()))
        seen = []
        other = observer.snapshot(self.db, 'c__d', 300, crawl_of(
            {'base_sha': 'x', 'issues': [], 'prs': []}, seen))
        self.assertEqual({'base_sha': 'x', 'issues': [], 'prs': []}, other)
        self.assertEqual(1, len(seen))
        # The first project's rows survived the second project's write.
        self.assertEqual(snapshot_of(), observer.snapshot(self.db, 'a__b', 300, failing_crawl(
            ValueError('must not crawl'))))

    def test_crawl_failure_keeps_the_previous_rows(self):
        observer.refresh(self.db, 'a__b', crawl_of(snapshot_of()))
        with self.assertRaises(ValueError):
            observer.refresh(self.db, 'a__b', failing_crawl(ValueError('network failed')))
        # The stored facts are untouched and still served within the TTL.
        self.assertEqual(snapshot_of(),
                         observer.snapshot(self.db, 'a__b', 300, failing_crawl(
                             AssertionError('must not crawl'))))

    def test_invalid_snapshot_is_rejected_without_writing(self):
        for bad in ({}, {'issues': [], 'prs': []}, 'nope'):
            with self.assertRaises(ValueError):
                observer.refresh(self.db, 'a__b', crawl_of(bad))
        for broken_item in ('not-a-dict', {'state': 'open'}, {'number': 'x'},
                            {'number': True}):
            with self.assertRaises(ValueError):
                observer.refresh(self.db, 'a__b', crawl_of(
                    {'base_sha': 's', 'issues': [broken_item], 'prs': []}))

    def test_corrupt_store_self_heals_through_a_crawl(self):
        observer.refresh(self.db, 'a__b', crawl_of(snapshot_of()))
        raw = sqlite3.connect(self.db)
        raw.execute("UPDATE items SET raw_json='{' WHERE project_key='a__b'")
        raw.commit()
        raw.close()
        seen = []
        healed = observer.snapshot(self.db, 'a__b', 300, crawl_of(snapshot_of(), seen))
        self.assertEqual(1, len(seen))
        self.assertEqual(snapshot_of(), healed)

    def test_iter_projects_lists_configs_sorted(self):
        root = Path(self.tmp.name) / 'projects'
        self.assertEqual([], observer.iter_projects(root))
        for key in ('b__c', 'a__b'):
            (root / key).mkdir(parents=True)
            (root / key / 'control-plane.json').write_text('{}', encoding='utf-8')
        (root / 'no-config').mkdir()
        self.assertEqual(['a__b', 'b__c'],
                         [p.parent.name for p in observer.iter_projects(root)])

    def test_refresh_all_isolates_failures(self):
        root = Path(self.tmp.name) / 'projects'
        for key in ('a__b', 'b__c'):
            (root / key).mkdir(parents=True)
            (root / key / 'control-plane.json').write_text('{}', encoding='utf-8')

        def factory(config_path):
            if config_path.parent.name == 'a__b':
                return failing_crawl(ValueError('boom'))
            return crawl_of(snapshot_of())

        results = observer.refresh_all(self.db, root, factory)
        self.assertEqual([('a__b', 'boom'), ('b__c', None)],
                         [(key, None if exc is None else str(exc)) for key, exc in results])
        # The healthy project's facts landed despite the broken sibling.
        self.assertEqual(snapshot_of(), observer.snapshot(self.db, 'b__c', 300,
                                                          failing_crawl(AssertionError('no crawl'))))

    def test_run_driver_refreshes_then_stops(self):
        root = Path(self.tmp.name) / 'projects'
        (root / 'a__b').mkdir(parents=True)
        (root / 'a__b' / 'control-plane.json').write_text('{}', encoding='utf-8')
        (root / 'b__c').mkdir(parents=True)
        (root / 'b__c' / 'control-plane.json').write_text('{}', encoding='utf-8')
        reported = []
        stop = threading.Event()

        def factory(config_path):
            if config_path.parent.name == 'a__b':
                return failing_crawl(ValueError('boom'))
            return crawl_of(snapshot_of())

        thread = threading.Thread(target=observer.run_driver, kwargs={
            'db_path': self.db, 'projects_root': root, 'crawl_factory': factory,
            'period': 0.05, 'stop': stop, 'report': lambda k, e: reported.append((k, e))})
        thread.start()
        try:
            deadline = time.time() + 10
            stored = None
            while time.time() < deadline:
                try:
                    stored = observer.snapshot(self.db, 'b__c', 300,
                                               failing_crawl(AssertionError('no crawl')))
                    break
                except Exception:
                    time.sleep(0.05)
            self.assertIsNotNone(stored, 'driver never refreshed the healthy project')
        finally:
            stop.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        # A raising project never crashed the loop, and every project is
        # reported each tick (the caller decides how loud to be).
        self.assertTrue(any(key == 'a__b' and isinstance(exc, ValueError) for key, exc in reported))
        self.assertTrue(any(key == 'b__c' and exc is None for key, exc in reported))

    def test_default_db_path_follows_the_data_home(self):
        home = str(Path(self.tmp.name) / 'home')
        previous = os.environ.get('CODE_LOOP_HOME')
        os.environ['CODE_LOOP_HOME'] = home
        try:
            self.assertEqual(Path(home) / 'observer.sqlite', observer.default_db_path())
        finally:
            if previous is None:
                os.environ.pop('CODE_LOOP_HOME', None)
            else:
                os.environ['CODE_LOOP_HOME'] = previous


if __name__ == '__main__':
    unittest.main()
