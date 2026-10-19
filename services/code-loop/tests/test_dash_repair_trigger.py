#!/usr/bin/env python3
"""The repair click (action='repair_round'): the review-round contract
re-pointed at the conflict-repair command.

Two faces, same as the review trigger: the endpoint contract (header, body
shape, error mapping, path→closure wiring) over an injected fake closure, and
the ledger behavior (idempotency, dispatch, status) over the real
coordinator/registry/ledger. The review-side matrix these reuse lives in
test_dashboard_serve.py — the shared POST/GET blocks make the two flavors
exercise the same code; this file pins only what the repair flavor adds or
re-points. Kept out of the pinned test_dashboard_serve.py on purpose (the
行数棘轮 only permits shrinkage there)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer

for _p in (str(Path(__file__).resolve().parents[1]),
           str(Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_dashboard_serve as tds
from codeloop.dash import serve, store


class RepairCommandLedgerTests(tds.MakeCommandTriggerTests):
    """Every inherited ledger case runs again with action='repair_round':
    same idempotency, same dispatch, same spawn-option validation — only the
    action string (and with it, per-action idempotency) differs."""

    ACTION = 'repair_round'

    def setUp(self):
        super().setUp()
        # re-point both closures at the repair action (parent builds the
        # review_round defaults)
        self.trigger = serve.make_command_trigger(self.db_path,
                                                  root=str(self.projects_root),
                                                  coordinator=self.coordinator,
                                                  action=self.ACTION)
        self.status = serve.make_command_status(self.db_path,
                                                root=str(self.projects_root),
                                                coordinator=self.coordinator,
                                                action=self.ACTION)

    def test_click_with_spawn_options_persists_and_dispatches_them(self):
        machine = self.enroll()
        conn = self.connect(machine)
        result = self.trigger('a__b', 88, agent='opencode',
                              model='kimi-for-coding/k3')
        self.assertEqual(('CLAIMED', 'dev-b'), (result['state'], result['machine']))
        command = conn.sent[0]['command']
        self.assertEqual(('repair_round', 88, 'opencode', 'kimi-for-coding/k3'),
                         (command['action'], command['number'],
                          command['agent'], command['model']))
        service = self.coordinator.service_for('a__b')
        try:
            row = service.latest_command(self.ACTION, 88)
            self.assertEqual(('opencode', 'kimi-for-coding/k3'),
                             (row['agent'], row['model']))
        finally:
            service.store.db.close()

    def test_actions_idempotent_separately(self):
        """A live review_round must not swallow a repair click for the same
        PR, nor vice versa — idempotency is per (action, number)."""
        review = serve.make_command_trigger(self.db_path,
                                            root=str(self.projects_root),
                                            coordinator=self.coordinator)
        first = review('a__b', 88)
        second = self.trigger('a__b', 88)
        self.assertNotEqual(first['command'], second['command'])
        self.assertTrue(second['created'])
        again = review('a__b', 88)
        self.assertEqual(first['command'], again['command'])
        self.assertFalse(again['created'])


class RepairTriggerEndpointTests(unittest.TestCase):
    """POST /api/repair/trigger and GET /api/repair/trigger/status: the
    repair paths reach their own closures through the same shared blocks —
    pin the wiring and the flavor-specific messages."""

    HEADER = {'X-Requested-With': 'dashboard-fetch'}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = store.connect(Path(self.tmp.name) / 'dash.sqlite')
        store.upsert_project(self.db, 'a__b', 'cp.json', enabled=1,
                             enabled_host='h1')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def serve_with(self, path='/api/repair/trigger', **kw):
        handler = serve.create(self.db, now=lambda: 0.0, **kw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server_thread = tds._serve(server)
        self.addCleanup(tds._stop, server, server_thread)
        base = 'http://127.0.0.1:%d' % server.server_port

        def call(method, url, payload=None, headers=None):
            request = Request(base + url, method=method,
                              data=None if payload is None else
                              json.dumps(payload).encode('utf-8'),
                              headers={'Content-Type': 'application/json',
                                       **({} if headers is None else headers)})
            try:
                with urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read().decode('utf-8'))
            except HTTPError as exc:
                return exc.code, json.loads(exc.read().decode('utf-8'))

        return call

    def test_happy_path_passthrough(self):
        seen = []

        def fake(org, number, agent, model):
            seen.append((org, number, agent, model))
            return {'ok': True, 'command': 7}

        call = self.serve_with(on_repair_trigger=fake)
        status, body = call('POST', '/api/repair/trigger',
                            {'project': 'a__b', 'number': 225}, self.HEADER)
        self.assertEqual(200, status)
        self.assertEqual({'ok': True, 'command': 7}, body)
        self.assertEqual([('a__b', 225, None, None)], seen)

    def test_not_wired_503(self):
        call = self.serve_with()
        status, body = call('POST', '/api/repair/trigger',
                            {'project': 'a__b'}, self.HEADER)
        self.assertEqual(503, status)
        self.assertEqual('repair trigger not wired on this server', body['error'])

    def test_requires_header(self):
        call = self.serve_with(on_repair_trigger=lambda p, n: {'ok': True})
        self.assertEqual(403, call('POST', '/api/repair/trigger',
                                   {'project': 'a__b'}, headers=None)[0])

    def test_bad_number_400(self):
        call = self.serve_with(on_repair_trigger=lambda p, n: {'ok': True})
        for bad in ('x', 0, -1, True):
            status, _ = call('POST', '/api/repair/trigger',
                             {'project': 'a__b', 'number': bad}, self.HEADER)
            self.assertEqual(400, status, 'number=%r' % bad)

    def test_unknown_post_path_404(self):
        call = self.serve_with(on_repair_trigger=lambda p, n: {'ok': True})
        for path in ('/api/merge/trigger', '/api/repair'):
            status, body = call('POST', path, {'project': 'a__b'}, self.HEADER)
            self.assertEqual((404, {'error': 'not found'}), (status, body), path)

    def test_status_endpoint_uses_the_repair_closure(self):
        seen = []

        def fake(org, number):
            seen.append((org, number))
            return {'command': 9, 'state': 'PENDING'}

        call = self.serve_with(on_repair_status=fake)
        status, body = call('GET', '/api/repair/trigger/status?project=a__b&number=225')
        self.assertEqual(200, status)
        self.assertEqual({'command': 9, 'state': 'PENDING'}, body)
        self.assertEqual([('a__b', 225)], seen)

    def test_status_endpoint_not_wired_503(self):
        call = self.serve_with()
        status, body = call('GET', '/api/repair/trigger/status?project=a__b&number=225')
        self.assertEqual((503, {'error': 'command status not wired here'}),
                         (status, body))


if __name__ == '__main__':
    unittest.main(verbosity=2)
