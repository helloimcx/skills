"""Webhook push tests: style rendering, signature dedup, failure isolation."""
import json
import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.dash import notify, store


def issue(n, **extra):
    base = dict(number=n, state='open', title='[❓needs-human] decide %d' % n, body='',
                labels=[], created_at='2026-09-01T00:00:00Z',
                updated_at='2026-09-01T00:00:00Z', closed_at=None,
                html_url='http://g/%d' % n, comments=[])
    base.update(extra)
    return base


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = store.connect(Path(self.tmp.name) / 'dash.sqlite')
        self.requests = []
        self.status = 200
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                outer.requests.append({'path': self.path, 'body': body.decode('utf-8')})
                self.send_response(outer.status)
                self.end_headers()
                self.wfile.write(b'{}')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = 'http://127.0.0.1:%d/hook' % self.server.server_port
        pid = store.upsert_project(self.db, 'a__b', 'cp.json', enabled=1,
                                   last_ok_collect_at=store.parse_ts('2026-09-07T23:00:00Z'))
        store.apply_snapshot(self.db, pid, {'issues': [issue(7)], 'prs': [],
                                            'base_sha': 'b'})
        self.now = store.parse_ts('2026-09-08T00:00:00Z')  # issue aged > 24h

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.db.close()
        self.tmp.cleanup()

    def config(self, style='wecom', webhook=None):
        return {'notify': {'webhook': webhook if webhook is not None else self.url,
                           'style': style}}

    def test_wecom_push_and_dedup(self):
        result = notify.push_new_alerts(self.db, self.config(), now=self.now)
        self.assertEqual(1, result['pushed'])
        self.assertEqual(1, len(self.requests))
        payload = json.loads(self.requests[0]['body'])
        self.assertEqual('text', payload['msgtype'])
        self.assertIn('Issue #7', payload['text']['content'])
        again = notify.push_new_alerts(self.db, self.config(), now=self.now)
        self.assertEqual(0, again['new_alerts'])
        self.assertEqual(0, again['pushed'])
        self.assertEqual(1, len(self.requests))

    def test_new_signature_pushes_again(self):
        notify.push_new_alerts(self.db, self.config(), now=self.now)
        store.apply_snapshot(self.db, 1, {'issues': [issue(8)], 'prs': [],
                                          'base_sha': 'b'})
        result = notify.push_new_alerts(self.db, self.config(), now=self.now)
        self.assertEqual(1, result['pushed'])
        self.assertIn('Issue #8', self.requests[-1]['body'])

    def test_no_webhook_records_seen_without_push(self):
        first = notify.push_new_alerts(self.db, {}, now=self.now)
        self.assertEqual(1, first['new_alerts'])
        self.assertEqual(0, first['pushed'])
        late = notify.push_new_alerts(self.db, self.config(), now=self.now)
        self.assertEqual(0, late['new_alerts'])
        self.assertEqual(0, late['pushed'])
        self.assertEqual([], self.requests)

    def test_raw_style_payload(self):
        notify.push_new_alerts(self.db, self.config(style='raw'), now=self.now)
        payload = json.loads(self.requests[0]['body'])
        self.assertIn('text', payload)
        self.assertIn('alerts', payload)

    def test_push_failure_reported_not_raised(self):
        self.status = 500
        result = notify.push_new_alerts(self.db, self.config(), now=self.now)
        self.assertIsNotNone(result['error'])
        self.assertEqual(0, result['pushed'])

    def test_busy_store_reports_error_instead_of_raising(self):
        """2026-09-14 collect starvation: a locked store during the dedup meta
        write must surface as note['error'] — never crash the collector after
        a successful collect."""
        original = store.set_meta

        def busy(db, key, value):
            raise sqlite3.OperationalError('database is locked')

        store.set_meta = busy
        try:
            result = notify.push_new_alerts(self.db, self.config(), now=self.now)
        finally:
            store.set_meta = original
        self.assertEqual(0, result['pushed'])
        self.assertIn('locked', result['error'])

    def test_resolved_then_reappearing_alert_pushes_again(self):
        notify.push_new_alerts(self.db, self.config(), now=self.now)
        store.apply_snapshot(self.db, 1, {'issues': [issue(7, state='closed')],
                                          'prs': [], 'base_sha': 'b'})
        notify.push_new_alerts(self.db, self.config(), now=self.now)  # resolves, prunes
        store.apply_snapshot(self.db, 1, {'issues': [issue(7)], 'prs': [],
                                          'base_sha': 'b'})
        result = notify.push_new_alerts(self.db, self.config(), now=self.now)
        self.assertEqual(1, result['pushed'])


    def test_dashboard_url_appends_deep_link(self):
        cfg = self.config()
        cfg['notify']['dashboard_url'] = 'http://127.0.0.1:8712'
        notify.push_new_alerts(self.db, cfg, now=self.now)
        body = self.requests[0]['body']
        # unmapped rules land on the kanban board (merged home view)
        self.assertIn('http://127.0.0.1:8712/#p=a__b&view=kanban', body)

    def test_collect_alerts_deep_link_to_projects_view(self):
        self.db.execute("UPDATE projects SET last_ok_collect_at=?",
                        (store.parse_ts('2026-09-01T00:00:00Z'),))
        cfg = self.config()
        cfg['notify']['dashboard_url'] = 'http://127.0.0.1:8712/'
        notify.push_new_alerts(self.db, cfg, now=self.now)
        body = self.requests[0]['body']
        self.assertIn('http://127.0.0.1:8712/#p=a__b&view=projects', body)

    def test_no_dashboard_url_keeps_message_plain(self):
        notify.push_new_alerts(self.db, self.config(), now=self.now)
        self.assertNotIn('#p=', self.requests[0]['body'])


if __name__ == '__main__':
    unittest.main()
