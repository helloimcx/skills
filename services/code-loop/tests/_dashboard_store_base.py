#!/usr/bin/env python3
"""Shared fixtures for dashboard store and board tests (issue #185 convention).

The board suite (test_dashboard_kanban.py) outgrew test_dashboard_store.py's
line-count ratchet baseline, so the suite moved out and the fixtures both files
share moved here — split, don't raise (precedent: _service_test_base.py). The
file name does not start with test_*, so pytest never collects it as a suite.
"""
import tempfile
import unittest
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.dash import store


def ts(day, hour=0, minute=0):
    return '2026-09-%02dT%02d:%02d:00Z' % (day, hour, minute)


def gitea_issue(n, **extra):
    base = dict(number=n, state='open', title='Issue %d' % n, body='',
                labels=[{'name': 'x'}], created_at=ts(1), updated_at=ts(1),
                closed_at=None, html_url='http://g/issue/%d' % n,
                comments=[])
    base.update(extra)
    return base


def gitea_pr(n, **extra):
    base = dict(number=n, state='open', title='PR %d' % n, body='Closes #%d' % (n - 100),
                labels=[], created_at=ts(2), updated_at=ts(2), closed_at=None,
                html_url='http://g/pr/%d' % n,
                head={'sha': 's%d' % n, 'ref': 'feature/x'},
                base={'sha': 'base', 'ref': 'main'}, merged=False,
                comments=[], reviews=[])
    base.update(extra)
    return base


def comment(body, day, hour=0, poster='reviewer'):
    return {'id': body, 'body': body, 'user': {'login': poster}, 'created_at': ts(day, hour)}


def agent_comment(body, day, hour=0, poster='loop', role='handler'):
    """A comment carrying the code-loop agent identity marker, as published by
    the Control Plane or mandated by the loop rules for direct CLI writes."""
    c = comment(body, day, hour, poster)
    c['body'] = body + '\n\n<!-- code-loop-agent: %s -->' % role
    return c


def epoch(day, hour=0):
    return store.parse_ts(ts(day, hour))


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'dash.sqlite'
        self.db = store.connect(self.path)
        self.now = epoch(20, 12)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def pid(self):
        return store.upsert_project(self.db, 'a__b', 'cp.json', platform_url='http://g', repo='a/b')

    def snap(self, issues=(), prs=()):
        return {'issues': list(issues), 'prs': list(prs), 'base_sha': 'base'}


