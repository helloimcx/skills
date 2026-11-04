"""HTTP facade tests: fixed read-only routes over an open store connection."""
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.cp.server import Coordinator
from codeloop.dash import serve, serving, store, verdicts


def _serve(httpd):
    """Start ``serve_forever`` on a daemon thread and return the thread.

    issue #216/#199：teardown 必须 shutdown → join → server_close 顺序化——
    Windows 上 ``SelectSelector`` 只捕 ``InterruptedError``，若
    ``server_close()`` 在 serve_forever 线程仍在 select 时关闭 socket，
    线程会以 WinError 10038（WSAENOTSOCK）崩溃；join 保证线程先退出。
    """
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return thread


def _stop(httpd, thread=None):
    """Ordered teardown for a server started by ``_serve``."""
    httpd.shutdown()
    if thread is not None:
        thread.join(timeout=10)
    httpd.server_close()


def _await_collect_idle(base, timeout=5):
    """issue #314：POST /api/collect 只提交后台单飞采集并立即返回；测试在
    断言采集效果前先等 running=False（完成栅栏——清快照发生在其之前）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with urlopen(base + '/api/collect/status', timeout=5) as response:
            if not json.loads(response.read().decode('utf-8'))['running']:
                return
        time.sleep(0.01)
    raise AssertionError('background collection did not finish in %ss' % timeout)


class FakeConnection:
    """A scripted WS agent socket: records frames, never touches the wire."""

    def __init__(self):
        self.sent = []

    def send_json(self, payload):
        self.sent.append(payload)

    def close(self):
        pass


class FakeService:
    """Records the trigger's control-plane calls and answers from a scripted
    state; real claim/bind/abandon semantics are covered against a real store
    in test_control_plane_service.py and the end-to-end case below."""

    def __init__(self, items=(), claim=None, claim_error=None, bind_error=None):
        self.items_list = list(items)
        self.claim_result = claim
        self.claim_error = claim_error
        self.bind_error = bind_error
        self.calls = []
        self.abandoned = []

    def claim(self, loop, worker, item_id=None, ttl=None):
        self.calls.append(('claim', loop, worker, item_id, ttl))
        if self.claim_error is not None:
            raise self.claim_error
        return self.claim_result

    def items(self, kind=None):
        self.calls.append(('items', kind))
        return self.items_list

    def bind(self, eid, token, worktree, branch=None):
        self.calls.append(('bind', eid, token, worktree, branch))
        if self.bind_error is not None:
            raise self.bind_error

    def abandon(self, eid, token):
        self.abandoned.append((eid, token))


class FakeGit:
    """Scripted git invocations: worktree preparation must never touch a real
    repository in a unit test. ``fail_on`` matches the first two argv tokens."""

    def __init__(self, head='', object_present=True, fail_on=()):
        self.head = head
        self.object_present = object_present
        self.fail_on = tuple(fail_on)
        self.calls = []

    def __call__(self, root, *args):
        self.calls.append((str(root),) + tuple(str(a) for a in args))
        key = ' '.join(str(a) for a in args[:2])
        if key in self.fail_on or (args and args[0] in self.fail_on):
            raise ValueError('GIT_FAILED: scripted failure in %s' % key)
        if args and args[0] == 'cat-file':
            if not self.object_present:
                raise ValueError('GIT_FAILED: missing object')
            return self.head
        if args and args[0] == 'rev-parse':
            return self.head
        return ''


def issue(n, **extra):
    base = dict(number=n, state='open', title='Issue %d' % n, body='',
                labels=[{'name': 'x'}], created_at='2026-09-01T00:00:00Z',
                updated_at='2026-09-01T00:00:00Z', closed_at=None,
                html_url='http://g/%d' % n, comments=[])
    base.update(extra)
    return base


def pr(n, **extra):
    base = dict(number=n, state='open', title='PR %d' % n, body='Closes #%d' % (n - 100),
                labels=[], created_at='2026-09-02T00:00:00Z',
                updated_at='2026-09-02T00:00:00Z', closed_at=None,
                html_url='http://g/pr/%d' % n,
                head={'sha': 's%d' % n, 'ref': 'feature/x'},
                base={'sha': 'base', 'ref': 'main'}, merged=False,
                comments=[], reviews=[])
    base.update(extra)
    return base


def comment(body, at, poster='rev'):
    return {'id': body, 'body': body, 'user': {'login': poster}, 'created_at': at}


class ServeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = store.connect(Path(self.tmp.name) / 'dash.sqlite')
        pid = store.upsert_project(self.db, 'a__b', 'cp.json', enabled=1,
                                   enabled_host='h1')
        store.apply_snapshot(self.db, pid, {
            'issues': [issue(1, title='[❓needs-human] decide', updated_at='2026-09-05T00:00:00Z')],
            'prs': [pr(101, comments=[comment('Approved for manual merge',
                                              '2026-09-03T00:00:00Z')])],
            'base_sha': 'b'})
        store.replace_runtime(self.db, pid, {
            'work_items': [], 'executions': [], 'leases': [], 'events': [],
            'outbox': [{'id': 7, 'execution_id': None, 'action': 'comment_issue',
                        'state': 'SENDING', 'receipt': None}]})
        self.now = store.parse_ts('2026-09-08T00:00:00Z')
        handler = serve.create(self.db, now=lambda: self.now,
                               on_reply=lambda org, num, body: {'id': 1,
                                                                'created_at': '2026-09-07T00:00:00Z'})
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self._serve_thread = _serve(self.server)
        self.base = 'http://127.0.0.1:%d' % self.server.server_port

    def tearDown(self):
        _stop(self.server, self._serve_thread)
        self.db.close()
        self.tmp.cleanup()

    def get(self, path, method='GET', headers=None):
        request = Request(self.base + path, method=method, headers=headers or {})
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def json(self, path):
        status, headers, body = self.get(path)
        self.assertEqual(200, status)
        self.assertTrue(headers.get('Content-Type', '').startswith('application/json'))
        return json.loads(body.decode('utf-8'))

    def post(self, path, headers=None):
        request = Request(self.base + path, method='POST', data=b'{}',
                          headers=headers or {})
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode('utf-8'))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode('utf-8'))

    def post_json(self, path, payload, headers=None):
        request = Request(self.base + path, method='POST',
                          data=json.dumps(payload).encode('utf-8'),
                          headers={'Content-Type': 'application/json',
                                   **({} if headers is None else headers)})
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode('utf-8'))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode('utf-8'))

    def test_reply_requires_header(self):
        self.assertEqual(403, self.post_json('/api/reply', {'project': 'a__b',
                                                            'number': 1, 'body': '1A'})[0])

    def test_reply_unknown_project(self):
        status, _ = self.post_json('/api/reply', {'project': 'zz__zz', 'number': 1,
                                                  'body': '1A'},
                                   {'X-Requested-With': 'dashboard-fetch'})
        self.assertEqual(404, status)

    def test_reply_empty_body(self):
        status, _ = self.post_json('/api/reply', {'project': 'a__b', 'number': 1,
                                                  'body': '   '},
                                   {'X-Requested-With': 'dashboard-fetch'})
        self.assertEqual(400, status)

    def test_reply_closed_issue(self):
        store.apply_snapshot(self.db, 1, {
            'issues': [issue(2, state='closed', title='[❓needs-human] old')],
            'prs': [], 'base_sha': 'b'})
        status, _ = self.post_json('/api/reply', {'project': 'a__b', 'number': 2,
                                                  'body': '1A'},
                                   {'X-Requested-With': 'dashboard-fetch'})
        self.assertEqual(409, status)

    def test_reply_happy_path_records_and_exits_queue(self):
        calls = []

        def fake_reply(org_repo, number, body):
            calls.append((org_repo, number, body))
            pid = store.upsert_project(self.db, org_repo, 'cp.json')
            store.record_user_reply(self.db, pid, number, body,
                                    '2026-09-07T00:00:00Z')
            return {'id': 99, 'created_at': '2026-09-07T00:00:00Z'}

        handler = serve.create(self.db, now=lambda: self.now, on_reply=fake_reply)
        self.server2 = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self._server2_thread = _serve(self.server2)
        base2 = 'http://127.0.0.1:%d' % self.server2.server_port

        import urllib.request as req
        request = req.Request(base2 + '/api/reply', method='POST',
                              data=json.dumps({'project': 'a__b', 'number': 1,
                                               'body': '1A 本期做'}).encode('utf-8'),
                              headers={'Content-Type': 'application/json',
                                       'X-Requested-With': 'dashboard-fetch'})
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode('utf-8'))
        self.assertEqual({'ok': True, 'comment_id': 99}, payload)
        self.assertEqual([('a__b', 1, '1A 本期做')], calls)
        # The reply is recorded -> the decision leaves the queue.
        rows = store.pending_decisions(self.db)
        self.assertEqual([], [r['number'] for r in rows])
        self.server2.shutdown()
        self._server2_thread.join(timeout=10)
        self.server2.server_close()

    def test_reply_platform_failure_maps_to_502(self):
        def failing_reply(org_repo, number, body):
            raise RuntimeError('gitea down')

        handler = serve.create(self.db, now=lambda: self.now, on_reply=failing_reply)
        self.server2 = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self._server2_thread = _serve(self.server2)
        base2 = 'http://127.0.0.1:%d' % self.server2.server_port

        import urllib.request as req
        request = req.Request(base2 + '/api/reply', method='POST',
                              data=json.dumps({'project': 'a__b', 'number': 1,
                                               'body': '1A'}).encode('utf-8'),
                              headers={'Content-Type': 'application/json',
                                       'X-Requested-With': 'dashboard-fetch'})
        try:
            with urlopen(request, timeout=5) as response:
                status = response.status
        except HTTPError as exc:
            status = exc.code
        self.assertEqual(502, status)
        self.server2.shutdown()
        self._server2_thread.join(timeout=10)
        self.server2.server_close()

    def test_reply_non_dict_json_body(self):
        request = Request(self.base + '/api/reply', method='POST',
                          data=b'[1,2,3]',
                          headers={'Content-Type': 'application/json',
                                   'X-Requested-With': 'dashboard-fetch'})
        try:
            with urlopen(request, timeout=5) as response:
                status = response.status
        except HTTPError as exc:
            status = exc.code
        self.assertEqual(400, status)

    def test_reply_boolean_number_rejected(self):
        status, _ = self.post_json('/api/reply', {'project': 'a__b', 'number': True,
                                                  'body': '1A'},
                                   {'X-Requested-With': 'dashboard-fetch'})
        self.assertEqual(400, status)

    def test_reply_not_wired_503(self):
        handler = serve.create(self.db, now=lambda: self.now)
        self.server2 = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self._server2_thread = _serve(self.server2)
        base2 = 'http://127.0.0.1:%d' % self.server2.server_port
        import urllib.request as req
        request = req.Request(base2 + '/api/reply', method='POST',
                              data=json.dumps({'project': 'a__b', 'number': 1,
                                               'body': '1A'}).encode('utf-8'),
                              headers={'Content-Type': 'application/json',
                                       'X-Requested-With': 'dashboard-fetch'})
        try:
            with urlopen(request, timeout=5) as response:
                status = response.status
        except HTTPError as exc:
            status = exc.code
        self.assertEqual(503, status)
        self.server2.shutdown()
        self._server2_thread.join(timeout=10)
        self.server2.server_close()

    def test_root_serves_html(self):
        status, headers, body = self.get('/')
        self.assertEqual(200, status)
        self.assertTrue(headers.get('Content-Type', '').startswith('text/html'))
        self.assertIn(b'<html', body)

    def test_overview(self):
        data = self.json('/api/overview')
        self.assertEqual(1, len(data['projects']))
        project = data['projects'][0]
        self.assertEqual('a__b', project['org_repo'])
        self.assertEqual(1, project['counts']['pending_merges'])
        self.assertEqual(1, project['counts']['pending_decisions'])
        self.assertEqual({'collect_never_ok', 'needs_human_aging', 'outbox_limbo'},
                         {a['rule'] for a in data['alerts']})

    def test_queue(self):
        data = self.json('/api/queue')
        self.assertEqual([101], [m['number'] for m in data['merges']])
        self.assertEqual([1], [d['number'] for d in data['decisions']])
        self.assertEqual(['outbox'], [e['type'] for e in data['exceptions']])
        self.assertIn('mergeable', data['merges'][0])

    def test_workers_route(self):
        data = self.json('/api/workers')
        self.assertEqual([], data['machines'])
        self.assertIn('window_days', data)
        self.assertIn('handler', data['thresholds'])

    def test_items_carry_verdict(self):
        data = self.json('/api/items')
        self.assertEqual(1, len(data['prs']))
        self.assertEqual('Approved for manual merge', data['prs'][0]['verdict'])
        self.assertEqual('a__b', data['issues'][0]['org_repo'])

    def test_persistent_items_hit_skips_aggregate_and_gzip_work(self):
        import gzip as gz
        baseline = self.get('/api/items')
        self.assertEqual(200, baseline[0])
        serving.precompute(self.db.execute(
            'PRAGMA database_list').fetchone()[2])
        with patch.object(serve, '_items', side_effect=AssertionError('aggregate ran')), \
                patch.object(serve.gzip, 'compress',
                             side_effect=AssertionError('gzip recompressed')):
            status, headers, body = self.get(
                '/api/items', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(200, status)
        self.assertEqual('gzip', headers.get('Content-Encoding'))
        self.assertEqual(json.loads(baseline[2].decode('utf-8')),
                         json.loads(gz.decompress(body).decode('utf-8')))

    def test_changed_source_never_falls_back_to_old_items_memory_snapshot(self):
        serving.precompute(self.db.execute(
            'PRAGMA database_list').fetchone()[2])
        self.db.execute("INSERT INTO issues(project_id,number,state,title) "
                        "VALUES (1,99,'open','arrived after precompute')")
        data = self.json('/api/items')
        self.assertIn(99, [row['number'] for row in data['issues']])

    def test_fresh_items_request_bypasses_persistent_cache(self):
        serving.precompute(self.db.execute(
            'PRAGMA database_list').fetchone()[2])
        with patch.object(serve, '_items', return_value={
                'issues': [], 'prs': [], 'fresh': True}):
            self.assertTrue(self.json('/api/items?fresh=1')['fresh'])

    def test_unreadable_disposable_cache_falls_back_to_live_builder(self):
        serving.precompute(self.db.execute(
            'PRAGMA database_list').fetchone()[2])
        self.db.execute('DROP TABLE serving_cache')
        data = self.json('/api/items')
        self.assertEqual([1], [row['number'] for row in data['issues']])
        self.assertEqual([101], [row['number'] for row in data['prs']])

    def test_gzip_response_when_client_accepts(self):
        """issue #159（用户决策 1A 2A）：Accept-Encoding: gzip 时读端点返回
        Content-Encoding: gzip 压缩体，解压后与明文 JSON 等价。回退不压缩
        时本测试变红（无 Content-Encoding 头）。"""
        import gzip as gz
        plain_status, plain_headers, plain_body = self.get('/api/items')
        self.assertEqual(200, plain_status)
        self.assertNotIn('Content-Encoding', plain_headers)
        gz_status, gz_headers, gz_body = self.get(
            '/api/items', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(200, gz_status)
        self.assertEqual('gzip', gz_headers.get('Content-Encoding'))
        self.assertLess(len(gz_body), len(plain_body),
                        'gzip 后载荷应小于明文（3.1MB JSON 压缩 ~10x）')
        self.assertEqual(json.loads(plain_body.decode('utf-8')),
                         json.loads(gz.decompress(gz_body).decode('utf-8')))
        # kanban 同批（决策 2A）
        kb_plain = self.get('/api/kanban')
        gz_kanban = self.get('/api/kanban', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(200, gz_kanban[0])
        self.assertEqual('gzip', gz_kanban[1].get('Content-Encoding'))
        self.assertEqual(json.loads(kb_plain[2].decode('utf-8')),
                         json.loads(gz.decompress(gz_kanban[2]).decode('utf-8')))

    def test_issue_detail_route(self):
        store.apply_snapshot(self.db, 1, {
            'issues': [issue(5, body='## 全文', comments=[
                comment('later', '2026-09-04T00:00:00Z'),
                comment('earlier', '2026-09-03T00:00:00Z')])],
            'prs': [], 'base_sha': 'b2'})
        data = self.json('/api/issue?project=a__b&number=5')
        self.assertEqual(5, data['number'])
        self.assertEqual('## 全文', data['body'])
        self.assertEqual(['earlier', 'later'], [c['body'] for c in data['comments']])
        self.assertEqual({'poster', 'body', 'created_at'}, set(data['comments'][0]))

    def test_issue_detail_unknown_project_or_number_404(self):
        self.assertEqual(404, self.get('/api/issue?project=zz__zz&number=1')[0])
        self.assertEqual(404, self.get('/api/issue?project=a__b&number=99')[0])

    def test_issue_detail_bad_number_400(self):
        self.assertEqual(400, self.get('/api/issue?project=a__b&number=abc')[0])
        self.assertEqual(400, self.get('/api/issue?project=a__b')[0])

    def test_static_vendor_served(self):
        for name in ('marked.min.js', 'purify.min.js'):
            status, headers, body = self.get('/static/' + name)
            self.assertEqual(200, status)
            self.assertTrue(headers.get('Content-Type', '').startswith('application/javascript'),
                            name)
            self.assertTrue(body.strip())

    def test_static_whitelist_only(self):
        self.assertEqual(404, self.get('/static/nope.js')[0])
        self.assertEqual(404, self.get('/static/../serve.py')[0])


    def test_kanban_route(self):
        data = self.json('/api/kanban')
        self.assertEqual({'todo', 'running', 'review', 'reviewing', 'ready',
                          'merged', 'blocked'}, set(data))
        self.assertEqual([101], [c['number'] for c in data['ready']])
        # PR cards carry the trigger outcome and the review-scope verdict on the
        # wire: the review column's button and its result line consume them.
        self.assertTrue({'trigger', 'review_scope', 'review_scope_note'}
                        <= set(data['ready'][0]))
        self.assertEqual({'decision', 'outbox'}, {c['card'] for c in data['blocked']})
        self.assertEqual([], data['review'])
        self.assertEqual([], data['running'])
        self.assertEqual([], data['reviewing'])

    def test_kanban_project_filter_and_unknown(self):
        data = self.json('/api/kanban?project=a__b')
        self.assertEqual([101], [c['number'] for c in data['ready']])
        self.assertEqual(404, self.get('/api/kanban?project=zz__zz')[0])

    def test_trends_days_param(self):
        self.assertEqual(14, len(self.json('/api/trends')['daily']))
        self.assertEqual(3, len(self.json('/api/trends?days=3')['daily']))

    def test_runtime_per_project(self):
        data = self.json('/api/runtime?project=a__b')
        self.assertEqual(['SENDING'], [o['state'] for o in data['outbox']])
        self.assertEqual(404, self.get('/api/runtime')[0])

    def test_unknown_route_and_method(self):
        self.assertEqual(404, self.get('/api/nope')[0])
        self.assertEqual(404, self.get('/api/overview', method='POST')[0])

    def test_collect_trigger_requires_header_and_handler(self):
        # No server-side collect wired -> 503 even with the right header.
        self.assertEqual(503, self.post('/api/collect', {'X-Requested-With': 'dashboard-fetch'})[0])
        self.assertEqual(403, self.post('/api/collect')[0])

    def test_collect_trigger_runs_handler(self):
        calls = []
        handler = serve.create(self.db, now=lambda: self.now,
                               on_collect=lambda only=None:
                                   (calls.append(only), {'ok': True})[1])
        self.server2 = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self._server2_thread = _serve(self.server2)
        import urllib.request as req
        request = req.Request('http://127.0.0.1:%d/api/collect' % self.server2.server_port,
                              method='POST', data=b'{}',
                              headers={'X-Requested-With': 'dashboard-fetch'})
        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode('utf-8'))
        self.assertEqual({'collect': {'started': True, 'project': None}}, payload)
        _await_collect_idle('http://127.0.0.1:%d' % self.server2.server_port)
        self.assertEqual([None], calls)
        self.server2.shutdown()
        self._server2_thread.join(timeout=10)
        self.server2.server_close()


    def test_events_route_groups_and_filters(self):
        pid = 1
        store.replace_runtime(self.db, pid, {
            'work_items': [], 'executions': [], 'leases': [], 'outbox': [],
            'cp_events': [{'id': 1, 'kind': 'execution.finished', 'payload': '{}',
                           'created_at': '2026-09-07T10:00:00Z'}]})
        data = self.json('/api/events?project=a__b&days=7')
        self.assertEqual(1, data['total'])
        by_date = {d['date']: d['count'] for d in data['daily']}
        self.assertEqual(1, by_date['2026-09-07'])
        unknown = self.get('/api/events?project=zz__zz')
        self.assertEqual(404, unknown[0])

    def test_trends_project_param(self):
        self.assertEqual(14, len(self.json('/api/trends?project=a__b')['daily']))
        self.assertEqual(404, self.get('/api/trends?project=zz__zz')[0])

    def test_collect_trigger_scopes_to_project(self):
        seen = []
        handler = serve.create(self.db, now=lambda: self.now,
                               on_collect=lambda only=None: seen.append(only) or {'ok': True})
        self.server2 = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self._server2_thread = _serve(self.server2)
        base2 = 'http://127.0.0.1:%d' % self.server2.server_port
        for path in ('/api/collect?project=a__b', '/api/collect'):
            request = Request(base2 + path, method='POST', data=b'{}',
                              headers={'X-Requested-With': 'dashboard-fetch'})
            with urlopen(request, timeout=5) as response:
                json.loads(response.read().decode('utf-8'))
            _await_collect_idle(base2)   # 串行两轮：上一轮完成才提交下一轮
        self.assertEqual(['a__b', None], seen)
        self.server2.shutdown()
        self._server2_thread.join(timeout=10)
        self.server2.server_close()

    def test_items_computes_cp_verdicts_once_per_project(self):
        """issue #130: _items used to rescan work_items per PR (553 scans)."""
        for i in range(2):
            pid = store.upsert_project(self.db, 'p%d__r%d' % (i, i), 'cp%d.json' % i)
            store.apply_snapshot(self.db, pid, {
                'issues': [], 'prs': [pr(100 + j) for j in range(3)], 'base_sha': 'b'})
        calls = []
        original = verdicts._cp_verdicts

        def counted(db, pid):
            calls.append(pid)
            return original(db, pid)

        verdicts._cp_verdicts = counted
        try:
            data = serve._items(self.db)
        finally:
            verdicts._cp_verdicts = original
        self.assertEqual(7, len(data['prs']))   # 1 (a__b) + 3 + 3
        self.assertEqual(3, len(calls))          # one per project, not per PR

    def test_snapshot_second_get_skips_recompute(self):
        calls = []
        original = store.overview

        def counted(db, **kw):
            calls.append(1)
            return original(db, **kw)

        store.overview = counted
        try:
            first = self.json('/api/overview')
            second = self.json('/api/overview')
        finally:
            store.overview = original
        self.assertEqual(1, len(calls))
        self.assertEqual(first, second)

    def test_snapshot_stale_entry_refreshes_in_background(self):
        calls = []
        gate = threading.Event()
        original = store.overview

        def counted(db, **kw):
            calls.append(1)
            if len(calls) > 1:
                gate.wait(5)  # block only the background recompute
            return original(db, **kw)

        store.overview = counted
        try:
            self.json('/api/overview')              # cold compute
            self.assertEqual(1, len(calls))
            self.now += serve.SNAPSHOT_REFRESH_SECONDS + 1
            started = time.time()
            status, _, _ = self.get('/api/overview')  # last-good served at once
            self.assertEqual(200, status)
            # The recompute blocks on the gate (~5s): a fast response proves the
            # cached value was served without waiting for it. The call count may
            # already have advanced concurrently, so it is not asserted here.
            self.assertLess(time.time() - started, 2)
            gate.set()
            deadline = time.time() + 5
            while len(calls) < 2 and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(2, len(calls))
        finally:
            gate.set()
            store.overview = original

    def test_post_clear_discards_inflight_recompute(self):
        """issue #167 回归：POST cache.clear() 时在途 recompute 不得回填旧数据。

        竞态窗口 = recompute 读库之后、写缓存之前。测试用 gate 把 recompute
        精确阻塞在「已读到旧值 v1、尚未写缓存」处 → POST collect（clear +
        generation++）→ 放行 gate → 无代际校验时 recompute 会把 v1 回填进
        缓存，下一次 GET 命中旧快照；有校验时代次已变 → 丢弃 → GET 重新
        计算返回 v2。"""
        calls = []
        gate = threading.Event()
        original = store.overview
        state = {'value': 'v1'}

        def counted(db, **kw):
            value = state['value']   # 读库（拿到当前快照值）
            calls.append(1)
            if len(calls) > 1:
                gate.wait(5)         # 阻塞在读库后、写缓存前（竞态窗口）
            return {'value': value}

        store.overview = counted
        try:
            handler = serve.create(self.db, now=lambda: self.now,
                                   on_collect=lambda only=None: {'ok': True})
            self.server2 = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            self._server2_thread = _serve(self.server2)
            base2 = 'http://127.0.0.1:%d' % self.server2.server_port
            with urlopen(base2 + '/api/overview', timeout=5) as response:
                first = json.loads(response.read().decode('utf-8'))
            self.assertEqual('v1', first['value'])
            self.assertEqual(1, len(calls))
            # 过期 → 触发后台 recompute：读到 v1 后阻塞在 gate（写缓存前）
            self.now += serve.SNAPSHOT_REFRESH_SECONDS + 1
            with urlopen(base2 + '/api/overview', timeout=5) as response:
                json.loads(response.read().decode('utf-8'))
            deadline = time.time() + 5
            while len(calls) < 2 and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(2, len(calls))   # recompute 已读旧值并阻塞在 gate
            # 平台事实已更新；POST collect：clear + generation++（在途 recompute 的
            # v1 快照必须被丢弃，不得回填）
            state['value'] = 'v2'
            request = Request(base2 + '/api/collect', method='POST', data=b'{}',
                              headers={'X-Requested-With': 'dashboard-fetch'})
            with urlopen(request, timeout=5) as response:
                json.loads(response.read().decode('utf-8'))
            _await_collect_idle(base2)   # 后台采集收尾（generation++）完成后再放行
            gate.set()   # 放行 recompute：无代际校验会把 v1 回填进缓存
            # 下一次 GET 必须返回新计算（v2），而非 recompute 回填的 v1
            deadline = time.time() + 5
            while len(calls) < 3 and time.time() < deadline:
                time.sleep(0.01)
            with urlopen(base2 + '/api/overview', timeout=5) as response:
                third = json.loads(response.read().decode('utf-8'))
            self.assertEqual('v2', third['value'],
                             'POST clear 后 GET 必须返回新数据，而非在途 recompute 回填的旧快照')
        finally:
            gate.set()
            store.overview = original
            self.server2.shutdown()
            self._server2_thread.join(timeout=10)
            self.server2.server_close()

    def test_trigger_clear_discards_inflight_recompute(self):
        """The click's pre-claim must show on the very next fetch: an in-flight
        recompute started before the click must not refill the snapshot."""
        calls = []
        gate = threading.Event()
        original = store.overview
        state = {'value': 'pre-claim'}

        def counted(db, **kw):
            value = state['value']
            calls.append(1)
            if len(calls) > 1:
                gate.wait(5)         # 阻塞在读库后、写缓存前（竞态窗口）
            return {'value': value}

        store.overview = counted
        try:
            handler = serve.create(self.db, now=lambda: self.now,
                                   on_review_trigger=lambda project, number, a, m: {'ok': True})
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            server_thread = _serve(server)
            base = 'http://127.0.0.1:%d' % server.server_port
            with urlopen(base + '/api/overview', timeout=5) as response:
                first = json.loads(response.read().decode('utf-8'))
            self.assertEqual('pre-claim', first['value'])
            self.now += serve.SNAPSHOT_REFRESH_SECONDS + 1
            with urlopen(base + '/api/overview', timeout=5) as response:
                json.loads(response.read().decode('utf-8'))
            deadline = time.time() + 5
            while len(calls) < 2 and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(2, len(calls))   # recompute 已读旧值并阻塞在 gate
            state['value'] = 'claimed'
            request = Request(base + '/api/review/trigger', method='POST',
                              data=b'{"project": "a__b", "number": 1}',
                              headers={'X-Requested-With': 'dashboard-fetch'})
            with urlopen(request, timeout=5) as response:
                json.loads(response.read().decode('utf-8'))
            gate.set()   # 放行 recompute：无代际校验会把旧值回填进缓存
            deadline = time.time() + 5
            while len(calls) < 3 and time.time() < deadline:
                time.sleep(0.01)
            with urlopen(base + '/api/overview', timeout=5) as response:
                body = json.loads(response.read().decode('utf-8'))
            self.assertEqual('claimed', body['value'],
                             '触发轮 POST 后 GET 必须返回新数据，而非在途 recompute 回填的旧快照')
        finally:
            gate.set()
            store.overview = original
            server.shutdown()
            server_thread.join(timeout=10)
            server.server_close()

    def test_snapshot_keeps_last_good_on_recompute_failure(self):
        original = store.overview
        state = {'fail': False}

        def flaky(db, **kw):
            if state['fail']:
                raise RuntimeError('boom')
            return original(db, **kw)

        store.overview = flaky
        try:
            good = self.json('/api/overview')
            self.now += serve.SNAPSHOT_REFRESH_SECONDS + 1
            state['fail'] = True
            status, _, body = self.get('/api/overview')
            self.assertEqual(200, status)   # last-good served, error swallowed
            self.assertEqual(good, json.loads(body.decode('utf-8')))
        finally:
            store.overview = original

    def test_fresh_get_bypasses_the_snapshot(self):
        """The click's post-action board refetch must not be answered from the
        last-good snapshot: the ledger row it was written for is newer than the
        cached body, and the 10s window would hide the failure bar. fresh=1
        neither reads nor writes the shared snapshot."""
        calls = []
        original = store.overview

        def counted(db, **kw):
            calls.append(1)
            return original(db, **kw)

        store.overview = counted
        try:
            self.json('/api/overview')
            self.json('/api/overview')
            self.assertEqual(1, len(calls))          # served from the snapshot
            self.json('/api/overview?fresh=1')
            self.assertEqual(2, len(calls))          # bypassed: recomputed
            self.json('/api/overview')
            self.assertEqual(2, len(calls))          # and the snapshot still holds
        finally:
            store.overview = original
        self.assertEqual(['/api/overview'],
                         list(self.server.RequestHandlerClass.snapshot_cache))

    def test_snapshot_only_caches_api_successes(self):
        self.get('/')                                    # html re-read per request
        self.get('/api/nope')                            # 404 not cached
        self.get('/api/issue?project=a__b&number=abc')   # 400 not cached
        self.assertEqual([], list(self.server.RequestHandlerClass.snapshot_cache))
        self.json('/api/overview')
        self.assertEqual(['/api/overview'],
                         list(self.server.RequestHandlerClass.snapshot_cache))

    def test_collect_clears_snapshot(self):
        calls = []
        original = store.overview

        def counted(db, **kw):
            calls.append(1)
            return original(db, **kw)

        store.overview = counted
        try:
            handler = serve.create(self.db, now=lambda: self.now,
                                   on_collect=lambda only=None: {'ok': True})
            self.server2 = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            self._server2_thread = _serve(self.server2)
            base2 = 'http://127.0.0.1:%d' % self.server2.server_port
            with urlopen(base2 + '/api/overview', timeout=5) as response:
                json.loads(response.read().decode('utf-8'))
            self.assertEqual(1, len(calls))
            request = Request(base2 + '/api/collect', method='POST', data=b'{}',
                              headers={'X-Requested-With': 'dashboard-fetch'})
            with urlopen(request, timeout=5) as response:
                json.loads(response.read().decode('utf-8'))
            _await_collect_idle(base2)
            with urlopen(base2 + '/api/overview', timeout=5) as response:
                json.loads(response.read().decode('utf-8'))
            self.assertEqual(2, len(calls))  # collect wiped the cache -> recompute
        finally:
            store.overview = original
            self.server2.shutdown()
            self._server2_thread.join(timeout=10)
            self.server2.server_close()

    def test_snapshot_works_with_default_now(self):
        """create() without an injectable clock must not TypeError on the
        snapshot staleness check (2026-09-14 review finding)."""
        handler = serve.create(self.db)
        self.server3 = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self._server3_thread = _serve(self.server3)
        base3 = 'http://127.0.0.1:%d' % self.server3.server_port
        for _ in range(2):
            with urlopen(base3 + '/api/overview', timeout=5) as response:
                self.assertEqual(200, response.status)
        self.server3.shutdown()
        self._server3_thread.join(timeout=10)
        self.server3.server_close()

    def test_reply_clears_snapshot(self):
        calls = []
        original = store.pending_decisions

        def counted(db, **kw):
            calls.append(1)
            return original(db, **kw)

        store.pending_decisions = counted
        try:
            handler = serve.create(self.db, now=lambda: self.now,
                                   on_reply=lambda org, num, body: {
                                       'id': 1, 'created_at': '2026-09-07T00:00:00Z'})
            self.server2 = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            self._server2_thread = _serve(self.server2)
            base2 = 'http://127.0.0.1:%d' % self.server2.server_port
            with urlopen(base2 + '/api/queue', timeout=5) as response:
                json.loads(response.read().decode('utf-8'))
            self.assertEqual(1, len(calls))
            request = Request(base2 + '/api/reply', method='POST',
                              data=json.dumps({'project': 'a__b', 'number': 1,
                                               'body': '1A'}).encode('utf-8'),
                              headers={'Content-Type': 'application/json',
                                       'X-Requested-With': 'dashboard-fetch'})
            with urlopen(request, timeout=5) as response:
                json.loads(response.read().decode('utf-8'))
            with urlopen(base2 + '/api/queue', timeout=5) as response:
                json.loads(response.read().decode('utf-8'))
            self.assertEqual(2, len(calls))  # reply wiped the cache -> fresh queue
        finally:
            store.pending_decisions = original
            self.server2.shutdown()
            self._server2_thread.join(timeout=10)
            self.server2.server_close()


class TriggerEndpointTests(unittest.TestCase):
    """POST /api/review/trigger: header/project/body contract and error
    mapping, over an injected fake trigger closure (never a real spawn)."""

    HEADER = {'X-Requested-With': 'dashboard-fetch'}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = store.connect(Path(self.tmp.name) / 'dash.sqlite')
        store.upsert_project(self.db, 'a__b', 'cp.json', enabled=1,
                             enabled_host='h1')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def serve_with(self, **kw):
        handler = serve.create(self.db, now=lambda: 0.0, **kw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server_thread = _serve(server)
        self.addCleanup(_stop, server, server_thread)
        base = 'http://127.0.0.1:%d' % server.server_port

        def post(payload, headers=None):
            request = Request(base + '/api/review/trigger', method='POST',
                              data=json.dumps(payload).encode('utf-8'),
                              headers={'Content-Type': 'application/json',
                                       **({} if headers is None else headers)})
            try:
                with urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read().decode('utf-8'))
            except HTTPError as exc:
                return exc.code, json.loads(exc.read().decode('utf-8'))

        return post

    def test_requires_header(self):
        post = self.serve_with(on_review_trigger=lambda p, n: {'ok': True})
        self.assertEqual(403, post({'project': 'a__b'}, headers=None)[0])

    def test_not_wired_503(self):
        post = self.serve_with()
        status, body = post({'project': 'a__b'}, headers=self.HEADER)
        self.assertEqual(503, status)
        self.assertEqual('review trigger not wired on this server', body['error'])

    def test_happy_path_passthrough(self):
        seen = []

        def fake(org, number, agent, model):
            seen.append((org, number))
            return {'ok': True, 'pid': 4242}

        post = self.serve_with(on_review_trigger=fake)
        status, body = post({'project': 'a__b', 'number': 88}, headers=self.HEADER)
        self.assertEqual(200, status)
        self.assertEqual({'ok': True, 'pid': 4242}, body)
        self.assertEqual([('a__b', 88)], seen)

    def test_number_optional_defaults_none(self):
        seen = []
        post = self.serve_with(on_review_trigger=lambda p, n, a, m: seen.append(n)
                               or {'ok': True})
        status, _ = post({'project': 'a__b'}, headers=self.HEADER)
        self.assertEqual(200, status)
        self.assertEqual([None], seen)

    def test_bad_number_400(self):
        post = self.serve_with(on_review_trigger=lambda p, n: {'ok': True})
        for bad in ('x', 0, -1, True, 1.5):
            status, _ = post({'project': 'a__b', 'number': bad}, headers=self.HEADER)
            self.assertEqual(400, status, 'number=%r' % bad)

    def test_action_error_maps_status(self):
        def refusing(org, number, agent, model):
            raise serve.ActionError(429, 'COOLDOWN')

        post = self.serve_with(on_review_trigger=refusing)
        status, body = post({'project': 'a__b'}, headers=self.HEADER)
        self.assertEqual(429, status)
        self.assertEqual({'error': 'COOLDOWN'}, body)

    def test_unexpected_failure_maps_502(self):
        def broken(org, number, agent, model):
            raise RuntimeError('boom')

        post = self.serve_with(on_review_trigger=broken)
        status, body = post({'project': 'a__b'}, headers=self.HEADER)
        self.assertEqual(502, status)
        self.assertIn('boom', body['error'])

    def test_spawn_options_pass_through_to_the_closure(self):
        seen = []
        post = self.serve_with(on_review_trigger=lambda p, n, a, m:
                               seen.append((p, n, a, m)) or {'ok': True})
        status, _ = post({'project': 'a__b', 'number': 88,
                          'agent': 'opencode', 'model': 'kimi-for-coding/k3'},
                         headers=self.HEADER)
        self.assertEqual(200, status)
        self.assertEqual([('a__b', 88, 'opencode', 'kimi-for-coding/k3')], seen)

    def test_spawn_options_omitted_stay_none(self):
        seen = []
        post = self.serve_with(on_review_trigger=lambda p, n, a, m:
                               seen.append((a, m)) or {'ok': True})
        status, _ = post({'project': 'a__b', 'number': 88}, headers=self.HEADER)
        self.assertEqual(200, status)
        self.assertEqual([(None, None)], seen)

    def test_spawn_options_malformed_shape_400(self):
        post = self.serve_with(on_review_trigger=lambda p, n, a, m: {'ok': True})
        for bad in ({'agent': 7}, {'agent': 'opencode', 'model': 3},
                    {'agent': ['opencode']}):
            status, _ = post(dict({'project': 'a__b', 'number': 88}, **bad),
                             headers=self.HEADER)
            self.assertEqual(400, status, repr(bad))


class SpawnOptionsEndpointTests(unittest.TestCase):
    """GET /api/spawn/options: the model list behind the trigger dialog. Its
    freshness belongs to its own detection cache — it must bypass the page
    snapshot cache so a fresh detection is visible on the next open."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = store.connect(Path(self.tmp.name) / 'dash.sqlite')
        store.upsert_project(self.db, 'a__b', 'cp.json', enabled=1,
                             enabled_host='h1')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def serve_with(self, **kw):
        handler = serve.create(self.db, now=lambda: 0.0, **kw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        server_thread = _serve(server)
        self.addCleanup(_stop, server, server_thread)
        base = 'http://127.0.0.1:%d' % server.server_port

        def get(path='/api/spawn/options'):
            try:
                with urlopen(base + path, timeout=5) as response:
                    return response.status, json.loads(response.read().decode('utf-8'))
            except HTTPError as exc:
                return exc.code, json.loads(exc.read().decode('utf-8'))

        return get

    def test_not_wired_503(self):
        get = self.serve_with()
        status, body = get()
        self.assertEqual(503, status)
        self.assertEqual('spawn options not wired on this server', body['error'])

    def test_payload_passthrough_and_no_snapshot_cache(self):
        calls = []

        def fake_options(project=''):
            calls.append(project)
            return {'ok': True, 'agents': [{'id': 'claude', 'models': None},
                                           {'id': 'opencode',
                                            'models': ['x/y' if len(calls) == 1 else 'z/w']}]}
        get = self.serve_with(on_spawn_options=fake_options)
        status, first = get()
        self.assertEqual(200, status)
        self.assertEqual(['x/y'], first['agents'][1]['models'])
        status, second = get()
        self.assertEqual(200, status)
        self.assertEqual(['z/w'], second['agents'][1]['models'])   # not cached
        self.assertEqual(2, len(calls))

    def test_spawn_options_passes_the_project_through(self):
        seen = []

        def fake_options(project=''):
            seen.append(project)
            return {'ok': True, 'agents': [], 'scope': {'kind': 'serve-host'}}

        get = self.serve_with(on_spawn_options=fake_options)
        status, body = get('/api/spawn/options?project=mocx__skills')
        self.assertEqual(200, status)
        self.assertEqual(['mocx__skills'], seen)
        self.assertEqual({'kind': 'serve-host'}, body['scope'])


class MakeCommandTriggerTests(unittest.TestCase):
    """The unified click: enqueue on the project's ledger + in-process
    dispatch to a live agent (real coordinator, real registry, real ledger
    here). The machine-side execution half lives in test_cp_agent.py; the
    old synchronous prepare-in-the-click behavior is gone by design."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._home = os.environ.get('CODE_LOOP_HOME')
        os.environ['CODE_LOOP_HOME'] = self.tmp.name
        self.projects_root = self.root / 'projects'
        cfg_dir = self.projects_root / 'a__b'
        cfg_dir.mkdir(parents=True)
        (cfg_dir / 'control-plane.json').write_text(json.dumps(
            {'platform': {'url': 'http://g', 'repo': 'a/b'}, 'remote': True,
             'state_dir': str(cfg_dir / 'state'), 'project_key': 'a__b',
             'snapshot_ttl': 3600}), encoding='utf-8')
        self.coordinator = Coordinator(str(self.projects_root),
                                       self.root / 'registry.sqlite')
        self.db_path = str(self.root / 'dash.sqlite')
        self.dash_db = store.connect(self.db_path)
        self.trigger = serve.make_command_trigger(self.db_path,
                                                 root=str(self.projects_root),
                                                 coordinator=self.coordinator)
        self.status = serve.make_command_status(self.db_path,
                                                root=str(self.projects_root),
                                                coordinator=self.coordinator)

    def tearDown(self):
        self.dash_db.close()
        import gc
        gc.collect()
        self.tmp.cleanup()
        if self._home is None:
            os.environ.pop('CODE_LOOP_HOME', None)
        else:
            os.environ['CODE_LOOP_HOME'] = self._home

    def enroll(self, name='dev-b'):
        registry = self.coordinator.registry()
        try:
            code = registry.create_enroll_code(name, ['a__b'])
            return registry.authenticate(registry.enroll(code, '1', '127.0.0.1')['token'])
        finally:
            registry.close()

    def connect(self, machine):
        conn = FakeConnection()
        self.coordinator.dispatcher.register(machine, conn)
        return conn

    def test_click_without_an_agent_queues_pending(self):
        result = self.trigger('a__b', 88)
        self.assertEqual({'ok': True, 'command': result['command'],
                          'state': 'PENDING', 'machine': None, 'created': True},
                         result)
        self.assertTrue(result['command'])

    def test_click_dispatches_to_a_live_agent_in_the_same_call(self):
        machine = self.enroll()
        conn = self.connect(machine)
        result = self.trigger('a__b', 88)
        self.assertEqual(('CLAIMED', 'dev-b'), (result['state'], result['machine']))
        self.assertEqual(1, len(conn.sent))
        self.assertEqual(88, conn.sent[0]['command']['number'])

    def test_click_with_spawn_options_persists_and_dispatches_them(self):
        machine = self.enroll()
        conn = self.connect(machine)
        result = self.trigger('a__b', 88, agent='opencode',
                              model='kimi-for-coding/k3')
        self.assertEqual(('CLAIMED', 'dev-b'), (result['state'], result['machine']))
        command = conn.sent[0]['command']
        self.assertEqual(('opencode', 'kimi-for-coding/k3'),
                         (command['agent'], command['model']))
        service = self.coordinator.service_for('a__b')
        try:
            row = service.latest_command('review_round', 88)
            self.assertEqual(('opencode', 'kimi-for-coding/k3'),
                             (row['agent'], row['model']))
        finally:
            service.store.db.close()

    def test_click_rejects_invalid_spawn_options(self):
        for kwargs in ({'agent': 'codex'}, {'model': 'x/y'},
                       {'agent': 'claude', 'model': 'x/y'},
                       {'agent': 'opencode', 'model': 'a b'}):
            with self.assertRaises(serve.ActionError) as caught:
                self.trigger('a__b', 88, **kwargs)
            self.assertEqual(400, caught.exception.status, repr(kwargs))

    def test_double_click_is_idempotent_while_live(self):
        first = self.trigger('a__b', 88)
        second = self.trigger('a__b', 88)
        self.assertEqual(first['command'], second['command'])
        self.assertFalse(second['created'])

    def test_invalid_number_refuses(self):
        for number in (None, 0, -1, 'x', True):
            with self.assertRaises(serve.ActionError) as caught:
                self.trigger('a__b', number)
            self.assertEqual(400, caught.exception.status)

    def test_unknown_project_refuses_404(self):
        with self.assertRaises(serve.ActionError) as caught:
            self.trigger('c__d', 88)
        self.assertEqual(404, caught.exception.status)

    def test_non_remote_project_still_gets_a_visible_row(self):
        """A project this server does not serve to workers still records the
        instruction: it queues, ages out with COMMAND_TTL, and the UI shows
        that — never a silent no-op."""
        cfg_dir = self.projects_root / 'c__d'
        cfg_dir.mkdir(parents=True)
        (cfg_dir / 'control-plane.json').write_text(json.dumps(
            {'platform': {'url': 'http://g', 'repo': 'c/d'}, 'remote': False,
             'state_dir': str(cfg_dir / 'state'), 'project_key': 'c__d',
             'snapshot_ttl': 3600}), encoding='utf-8')
        result = self.trigger('c__d', 7)
        self.assertEqual('PENDING', result['state'])
        self.assertIsNone(result['machine'])

    def test_status_reports_latest_command_and_flies_the_reason(self):
        self.assertEqual({'command': None}, self.status('a__b', 88))
        result = self.trigger('a__b', 88)
        view = self.status('a__b', 88)
        self.assertEqual(('PENDING', None, 0), (view['state'], view['machine'],
                                                view['attempts']))
        machine = self.enroll()
        self.connect(machine)
        self.trigger('a__b', 88)   # idempotent: same command, now dispatched
        service = self.coordinator.service_for('a__b')
        try:
            service.ack_command(result['command'], 'dev-b', 'FAILED',
                                fail_reason='CLAIM_UNAVAILABLE: 目标不可领取')
        finally:
            service.store.db.close()
        view = self.status('a__b', 88)
        self.assertEqual(('FAILED', 'CLAIM_UNAVAILABLE: 目标不可领取'),
                         (view['state'], view['fail_reason']))


if __name__ == '__main__':
    unittest.main()
