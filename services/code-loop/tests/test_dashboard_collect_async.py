"""issue #314 回归：POST /api/collect 必须把采集提交到后台单飞线程并立即返回。

同步时代请求线程会被分钟级采集独占（GIL + sqlite），连带拖垮同进程的
8722 控制面线程——看板周期性整页冻结、点击被静默丢弃的根因。
契约：started/skipped 立即返回；GET /api/collect/status 可轮询完成；
失败保持 last-good 快照并把原因记入 last_error。"""
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import sys as _sys
from pathlib import Path as _Path

if str(_Path(__file__).resolve().parents[1]) not in _sys.path:
    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

from codeloop.dash import serve, store


def _serve(httpd):
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return thread


class CollectAsyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = store.connect(Path(self.tmp.name) / 'collect.sqlite')
        self.now = store.parse_ts('2026-09-08T00:00:00Z')
        self.server = None

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
            self._thread.join(timeout=10)
            self.server.server_close()
        self.db.close()
        self.tmp.cleanup()

    def _server(self, on_collect):
        handler = serve.create(self.db, now=lambda: self.now, on_collect=on_collect)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self._thread = _serve(self.server)
        return 'http://127.0.0.1:%d' % self.server.server_port

    def _post(self, base, path='/api/collect'):
        request = Request(base + path, method='POST', data=b'{}',
                          headers={'X-Requested-With': 'dashboard-fetch'})
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode('utf-8'))

    def _status(self, base):
        with urlopen(base + '/api/collect/status', timeout=5) as response:
            return json.loads(response.read().decode('utf-8'))

    def _await_idle(self, base, timeout=5):
        """等后台采集收尾——只轮询状态端点，不固定睡眠，无竞态。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._status(base)['running']:
                return
            time.sleep(0.01)
        self.fail('background collection did not finish in %ss' % timeout)

    def test_submit_returns_before_collection_finishes(self):
        """POST 必须在采集仍被 gate 挡在后台时就返回（同步实现在此挂死）。"""
        gate = threading.Event()
        calls = []
        base = self._server(lambda only=None:
                            (calls.append(only), gate.wait(5))[1] or {'ok': True})
        payload = self._post(base)
        self.assertEqual({'collect': {'started': True, 'project': None}}, payload)
        self.assertTrue(self._status(base)['running'])
        gate.set()
        self._await_idle(base)
        self.assertEqual([None], calls)   # 恰好一轮，没有第二趟

    def test_single_flight_merges_repeat_triggers(self):
        """采集中再触发立即 skipped：不排队、不叠加，两 POST 一轮采集。"""
        gate = threading.Event()
        calls = []
        base = self._server(lambda only=None:
                            (calls.append(only), gate.wait(5))[1] or {'ok': True})
        self.assertEqual({'collect': {'started': True, 'project': None}},
                         self._post(base))
        deadline = time.time() + 5
        while not calls and time.time() < deadline:
            time.sleep(0.01)   # 第一轮确已停在采集内
        self.assertEqual({'collect': {'skipped': 'collect in progress'}},
                         self._post(base))
        gate.set()
        self._await_idle(base)
        self.assertEqual([None], calls)

    def test_failure_keeps_last_good_and_records_error(self):
        """采集失败：不清快照（last-good 保持）、last_error 记因、running 复位。"""
        original = store.overview
        store.overview = lambda db, **kw: {'value': 'v1'}

        def failing(only=None):
            raise RuntimeError('boom')

        try:
            base = self._server(failing)
            with urlopen(base + '/api/overview', timeout=5) as response:
                self.assertEqual('v1', json.loads(
                    response.read().decode('utf-8'))['value'])
            self._post(base)
            self._await_idle(base)
            status = self._status(base)
            self.assertIn('boom', status['last_error'])
            self.assertFalse(status['running'])
            with urlopen(base + '/api/overview', timeout=5) as response:
                self.assertEqual('v1', json.loads(
                    response.read().decode('utf-8'))['value'])
        finally:
            store.overview = original

    def test_success_clears_snapshot_after_background_completion(self):
        """收尾语义不丢：后台采集成功后仍要清快照（generation++），下一次
        GET 立即重算出新值，而不是继续吃 10s last-good。"""
        original = store.overview
        state = {'value': 'v1'}
        calls = []

        def overview(db, **kw):
            calls.append(state['value'])
            return {'value': state['value']}

        store.overview = overview
        try:
            base = self._server(lambda only=None: {'ok': True})
            with urlopen(base + '/api/overview', timeout=5) as response:
                self.assertEqual('v1', json.loads(
                    response.read().decode('utf-8'))['value'])
            state['value'] = 'v2'
            self._post(base)
            self._await_idle(base)
            with urlopen(base + '/api/overview', timeout=5) as response:
                self.assertEqual('v2', json.loads(
                    response.read().decode('utf-8'))['value'])
        finally:
            store.overview = original

    def test_status_reflects_running_immediately(self):
        """status 必须绕过快照缓存：POST 后第一个 GET 就能看到 running。"""
        gate = threading.Event()
        base = self._server(lambda only=None: gate.wait(5) or {'ok': True})
        self.assertFalse(self._status(base)['running'])   # 预热（若走快照会被钉住）
        self._post(base)
        self.assertTrue(self._status(base)['running'],
                        'collect/status 被快照缓存挡住：POST 后仍读到 running=False')
        gate.set()
        self._await_idle(base)


if __name__ == '__main__':
    unittest.main()
