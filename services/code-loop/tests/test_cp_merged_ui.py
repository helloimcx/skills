#!/usr/bin/env python3
"""The merged runtime: cp_server serves the operator UI on a loopback
listener of the same process. UI HTTP contract is byte-identical to the old
standalone dashboard (same handler factory), so the tests here assert the
mounting — loopback-only binding, live routes on the merged process, and the
retired standalone entry's refusal."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

import dashboard
import cp_server
from codeloop.cp.server import Coordinator
from codeloop.dash import collect
from codeloop.operator import face as operator_face


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as response:
        return response.status, json.loads(response.read().decode('utf-8'))


def post(base, path, payload, csrf=True):
    body = json.dumps(payload).encode('utf-8')
    request = urllib.request.Request(base + path, data=body, method='POST')
    if csrf:
        request.add_header('X-Requested-With', 'dashboard-fetch')
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode('utf-8'))


class MergedUiListenerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._code_loop_home = os.environ.get('CODE_LOOP_HOME')
        os.environ['CODE_LOOP_HOME'] = str(self.root / 'home')
        self._operator_wired = set(operator_face.WIRED)
        self._operator_executor = operator_face._EXECUTOR
        self._db = collect.DEFAULT_DB
        collect.DEFAULT_DB = self.root / 'dash.sqlite'
        (self.root / 'projects').mkdir()
        self.settings = {'projects_root': str(self.root / 'projects'),
                         'ui_host': '127.0.0.1', 'ui_port': 0}
        self.coordinator = Coordinator(self.root / 'projects',
                                       self.root / 'registry.sqlite')

    def tearDown(self):
        collect.DEFAULT_DB = self._db
        operator_face.WIRED.clear()
        operator_face.WIRED.update(self._operator_wired)
        operator_face._EXECUTOR = self._operator_executor
        if self._code_loop_home is None:
            os.environ.pop('CODE_LOOP_HOME', None)
        else:
            os.environ['CODE_LOOP_HOME'] = self._code_loop_home
        self.tmp.cleanup()

    def test_ui_serves_views_from_the_merged_process(self):
        # Force the operator-enabled branch even on machines where its local
        # dashboard config is absent. _ui_listener wires module-global face
        # state, so tearDown must restore it before sibling test modules run.
        with patch.object(cp_server, '_dash_config',
                          return_value={'operator': {'enabled': True}}), \
                patch.object(cp_server.operator_wiring, 'start_consumer',
                             return_value=(None, None)):
            httpd, db = cp_server._ui_listener(self.settings, self.coordinator)
        self.assertIsNotNone(httpd)
        try:
            self.assertEqual('127.0.0.1', httpd.server_address[0])
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            base = 'http://127.0.0.1:%d' % httpd.server_address[1]
            status, payload = get(base, '/api/overview')
            self.assertEqual(200, status)
            self.assertIn('projects', payload)
            with urllib.request.urlopen(base + '/', timeout=5) as response:
                self.assertEqual(200, response.status)
                self.assertTrue(response.read().startswith(b'<!doctype html'))
            # the whitelisted POST actions live here too — same CSRF gate
            status, payload = post(base, '/api/review/trigger',
                                   {'project': 'a__b', 'number': 88})
            self.assertEqual(404, status)          # unknown project, post-CSRF
            status, payload = post(base, '/api/review/trigger',
                                   {'project': 'a__b', 'number': 88}, csrf=False)
            self.assertEqual(403, status)
            self.assertIn('X-Requested-With', payload['error'])
        finally:
            if httpd is not None:
                httpd.server_close()
            if db is not None:
                db.close()

    def test_busy_ui_port_degrades_loudly_without_killing_coordination(self):
        # Occupy the port first (as a leftover standalone dashboard would).
        blocker = ThreadingHTTPServer(('127.0.0.1', 0), lambda *a: None)
        self.settings['ui_port'] = blocker.server_address[1]
        threading.Thread(target=blocker.serve_forever, daemon=True).start()
        try:
            httpd, db = cp_server._ui_listener(self.settings, self.coordinator)
            self.assertIsNone(httpd)
            self.assertIsNone(db)
        finally:
            blocker.server_close()

    def test_non_loopback_ui_host_is_refused(self):
        """The UI has no authentication of its own (its POST surface is a
        platform write + command enqueue): a non-loopback binding is refused
        loudly instead of publishing it to the LAN."""
        self.settings['ui_host'] = '0.0.0.0'
        httpd, db = cp_server._ui_listener(self.settings, self.coordinator)
        self.assertIsNone(httpd)
        self.assertIsNone(db)

    def test_ui_port_null_disables_the_listener(self):
        self.settings['ui_port'] = None
        httpd, db = cp_server._ui_listener(self.settings, self.coordinator)
        self.assertIsNone(httpd)
        self.assertIsNone(db)


class RetiredStandaloneServeTests(unittest.TestCase):
    def test_dashboard_serve_stub_refuses_with_a_pointer(self):
        import subprocess
        script = Path(dashboard.__file__)
        out = subprocess.run([sys.executable, str(script), 'serve'],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(2, out.returncode)
        self.assertIn('DASHBOARD_SERVE_MERGED', out.stdout)
        self.assertIn('cp_server.py serve', out.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
