"""Connection ownership regressions; never race a real shared SQLite handle."""
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from codeloop.dash import serve, store


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'dashboard #?%.sqlite'
        self.db = store.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.execute("INSERT INTO projects (org_repo) VALUES ('a__b')")
        self.db.execute("INSERT INTO issues (project_id,number,state) VALUES (1,1,'open')")
        self.clock = 1000
        self.records = []
        self.records_lock = threading.Lock()

    def factory(self):
        raw = store.connect_readonly(self.path)
        record = {'opened': threading.get_ident(), 'closed': None}
        with self.records_lock:
            self.records.append(record)

        class Tracked:
            def execute(inner, *args):
                self.assertEqual(record['opened'], threading.get_ident())
                return raw.execute(*args)

            def close(inner):
                self.assertEqual(record['opened'], threading.get_ident())
                raw.close()
                record['closed'] = threading.get_ident()
        return Tracked()

    def start(self, **kwargs):
        server = ThreadingHTTPServer(('127.0.0.1', 0),
                                     serve.create(self.db, now=lambda: self.clock, **kwargs))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            thread.join(5)
            server.server_close()
        self.addCleanup(stop)
        self.base = 'http://127.0.0.1:%d' % server.server_port
        return server.RequestHandlerClass

    def request(self, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.base + path, data=data,
                          headers={'X-Requested-With': 'dashboard-fetch'})
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            return response.code, json.loads(response.read())

    def assert_closed(self, count):
        self.assertEqual(count, len(self.records))
        self.assertTrue(all(r['opened'] == r['closed'] for r in self.records))

    def test_readonly_is_thread_affine_and_does_not_create_or_write(self):
        source = store.connect_readonly(self.path)
        self.addCleanup(source.close)
        self.assertEqual('a__b', source.execute('SELECT org_repo FROM projects').fetchone()['org_repo'])
        with self.assertRaises(sqlite3.OperationalError):
            source.execute('DELETE FROM projects')
        missing = self.path.parent / 'missing.sqlite'
        with self.assertRaises(sqlite3.OperationalError):
            store.connect_readonly(missing)
        self.assertFalse(missing.exists())
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.assertRaises(sqlite3.ProgrammingError):
                pool.submit(source.execute, 'SELECT 1').result(5)

    def test_parallel_requests_have_independent_connections(self):
        barrier = threading.Barrier(4)

        def route(db, *args):
            barrier.wait(3)
            return 'application/json', 200, {'n': db.execute('SELECT count(*) FROM projects').fetchone()[0]}

        self.start(db_factory=self.factory)
        with patch.object(serve, 'route', side_effect=route):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(self.request, ['/api/overview?fresh=1'] * 4))
        self.assertEqual([(200, {'n': 1})] * 4, results)
        self.assert_closed(4)
        self.assertEqual(4, len({r['opened'] for r in self.records}))
        self.assertEqual(1, self.db.execute('SELECT count(*) FROM projects').fetchone()[0])

    def test_kanban_uses_verdict_map_for_each_project(self):
        # PR number 1 collides with project id 1. Passing the outer
        # {project: {number: verdict}} map used to treat a dict as a verdict.
        self.db.execute("INSERT INTO prs (project_id,number,state,title) VALUES (1,1,'open','PR one')")
        self.start(db_factory=self.factory)
        status, board = self.request('/api/kanban?fresh=1')
        self.assertEqual(200, status, board)
        self.assertEqual(1, board['review'][0]['number'])
        self.assert_closed(1)

    def test_failed_request_closes_owned_connection(self):
        self.start(db_factory=self.factory)
        with patch.object(serve, 'route', side_effect=ValueError('bad query')):
            self.assertEqual((400, {'error': 'bad query'}), self.request('/api/overview?fresh=1'))
        self.assert_closed(1)

    def test_background_refresh_owns_connection_and_keeps_cached_response(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        calls = []
        original_factory = self.factory

        def factory():
            db = original_factory()
            original_close = db.close

            def close():
                original_close()
                closed.set()
            db.close = close
            return db

        def route(db, *args):
            calls.append(db)
            db.execute('SELECT 1').fetchone()
            if len(calls) > 1:
                entered.set()
                self.assertTrue(release.wait(4))
                raise RuntimeError('refresh failed')
            return 'application/json', 200, {'version': 1}

        self.start(db_factory=factory)
        with patch.object(serve, 'route', side_effect=route):
            self.assertEqual((200, {'version': 1}), self.request('/api/overview'))
            closed.clear()
            self.clock += serve.SNAPSHOT_REFRESH_SECONDS + 1
            try:
                self.assertEqual((200, {'version': 1}), self.request('/api/overview'))
                self.assertTrue(entered.wait(2))
            finally:
                release.set()
            self.assertTrue(closed.wait(3))
        self.assert_closed(2)
        self.assertIsNot(calls[0], calls[1])

    def test_post_validation_uses_factory_and_releases_before_callback(self):
        def reply(*args):
            self.assert_closed(1)
            return {'ok': True}

        self.start(db_factory=self.factory, on_reply=reply, on_collect=lambda _: None)
        self.assertEqual(200, self.request('/api/reply',
                         {'project': 'a__b', 'number': 1, 'body': 'answer'})[0])
        self.assert_closed(1)
        self.assertEqual(404, self.request('/api/collect?project=missing', {})[0])
        self.assert_closed(2)
        self.assertEqual(404, self.request('/api/reply',
                         {'project': 'missing', 'number': 1, 'body': 'answer'})[0])
        self.assert_closed(3)

    def test_merged_listener_wires_readonly_factory(self):
        import cp_server
        from codeloop.dash import collect
        from codeloop.cp.server import Coordinator
        with patch.object(collect, 'DEFAULT_DB', self.path), \
                patch.object(cp_server, '_dash_config', return_value=None):
            server, initial_db = cp_server._ui_listener(
                {'ui_port': 0, 'projects_root': str(self.path.parent / 'projects')},
                Coordinator(self.path.parent / 'projects', self.path.parent / 'registry.sqlite'))
        self.assertIsNotNone(server)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.base = 'http://127.0.0.1:%d' % server.server_port
            from codeloop.dash.schema import connect_readonly
            with patch.object(store, 'connect_readonly', wraps=connect_readonly) as opener:
                self.assertEqual(200, self.request('/api/overview?fresh=1')[0])
                self.assertEqual(1, opener.call_count)
            self.assertEqual(1, initial_db.execute('SELECT count(*) FROM projects').fetchone()[0])
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()
            initial_db.close()

    def test_legacy_connection_serializes_complete_route_without_closing_it(self):
        entered, release, overlapped = threading.Event(), threading.Event(), threading.Event()
        lock = threading.Lock()
        active = [0]

        def route(db, *args):
            with lock:
                active[0] += 1
                if active[0] > 1:
                    overlapped.set()
            entered.set()
            try:
                release.wait(3)
                return 'application/json', 200, {'ok': True}
            finally:
                with lock:
                    active[0] -= 1

        self.start()
        with patch.object(serve, 'route', side_effect=route):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.request, '/api/overview?fresh=1')
                self.assertTrue(entered.wait(2))
                second = pool.submit(self.request, '/api/overview?fresh=1')
                try:
                    self.assertFalse(overlapped.wait(0.2), 'legacy connection accessed concurrently')
                finally:
                    release.set()
                self.assertEqual(200, first.result(4)[0])
                self.assertEqual(200, second.result(4)[0])
        self.assertEqual(1, self.db.execute('SELECT count(*) FROM projects').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
