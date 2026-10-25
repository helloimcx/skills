"""PR3 surface: /api/operator ledger endpoint, operator trigger/options
closures, serve-side executor wiring, coordinator event sink."""
import json
import shutil
import sqlite3
import sys as _sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

for _p in (str(Path(__file__).resolve().parents[1]),
           str(Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.dash import schema, serve
from codeloop.operator import config as op_config
from codeloop.operator import face, ingest as op_ingest
from codeloop.operator import store as op_store
from codeloop.operator import wiring


def cfg():
    return op_config.load_operator_config({'operator': {'enabled': True}})


class OperatorLedgerEndpointTests(unittest.TestCase):
    def test_ledger_rows_from_mirror(self):
        home = tempfile.mkdtemp(prefix='op-api-')
        db = schema.connect(Path(home) / 'dash.sqlite')
        try:
            store = op_store.open_store(home)
            item, _ = op_store.put_signal(store, source='alert', kind='loop_stall',
                                          key='k1', payload={'message': 'stall'},
                                          org_repo='m')
            store.db.execute('''INSERT INTO executions (id,item_id,worker,token,
                expires_at,state,item_version,created_at)
                VALUES ('e1',?,'H/operator',7,?, 'RUNNING','v',?)''',
                             (item['id'], time.time() + 60, time.time()))
            from codeloop.dash import collect
            collect.collect_operator(db, home=home)
            store.db.close()
            payload = serve.operator_ledger(db)
            self.assertTrue(payload['ok'])
            self.assertEqual(payload['total'], 1)
            row = payload['rows'][0]
            self.assertEqual(row['id'], item['id'])
            self.assertEqual(row['signal']['kind'], 'loop_stall')
            self.assertEqual(row['signal']['message'], 'stall')
            self.assertEqual(row['run_state'], 'RUNNING')
            self.assertIsNone(row['outcome'])
        finally:
            db.close()

    def test_ledger_bounded_by_limit(self):
        """Review MINOR: the ledger must stay bounded — rows read are capped
        at `limit` (default 50), while total reports the full count and the
        page stays newest-first."""
        home = tempfile.mkdtemp(prefix='op-api2-')
        db = schema.connect(Path(home) / 'dash.sqlite')
        try:
            store = op_store.open_store(home)
            for i in range(3):
                op_store.put_signal(store, source='alert', kind='loop_stall',
                                    key='k%d' % i, payload={'message': 'stall'},
                                    org_repo='m', now=time.time() + i)
            from codeloop.dash import collect
            collect.collect_operator(db, home=home)
            store.db.close()
            capped = serve.operator_ledger(db, {'limit': '2'})
            self.assertTrue(capped['ok'])
            self.assertEqual(capped['total'], 3)
            self.assertEqual(len(capped['rows']), 2)
            self.assertEqual(capped['rows'][0]['signal']['key'], 'k2')
        finally:
            db.close()


class ManualTriggerTests(unittest.TestCase):
    def test_manual_bypasses_cooldown_and_cap(self):
        home = tempfile.mkdtemp(prefix='op-trig-')
        store = op_store.open_store(home)
        try:
            trigger = wiring.make_manual_trigger(store, cfg(), dispatcher=None,
                                                 home=home)
            first = trigger('mocx__skills', None, None, None)
            self.assertTrue(first['ok'])
            self.assertTrue(first['id'].startswith('operator:'))
            # second manual within cooldown still fires (bypass), distinct key
            second = trigger('a__b', 88, 'opencode', 'model-k3')
            self.assertTrue(second['ok'])
            # the trigger's provenance is recorded on the fired item
            row = store.db.execute(
                "SELECT payload FROM work_items WHERE key LIKE 'op:manual:manual|%' "
                'ORDER BY created_at DESC LIMIT 1').fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(json.loads(row['payload'])['project'], 'a__b')
            self.assertEqual(json.loads(row['payload'])['number'], 88)
        finally:
            store.db.close()

    def test_active_round_dedup_kept(self):
        home = tempfile.mkdtemp(prefix='op-trig2-')
        store = op_store.open_store(home)
        try:
            #同一 manual key 不复现（key 带时间戳），验证 fired 命令都在一条
            trigger = wiring.make_manual_trigger(store, cfg(), dispatcher=None,
                                                 home=home)
            trigger('a__b', 3, None, None)
            pending = store.pending_commands()
            self.assertEqual([c['action'] for c in pending], ['operator_round'])
        finally:
            store.db.close()


class OperatorTriggerEndpointTests(unittest.TestCase):
    """POST /api/operator/trigger over a real socket: the shared serve POST
    block calls the closure as wired(project, number, agent, model) — the
    regression the closure unit tests cannot see (review MAJOR: every click
    used to 502 TypeError)."""

    HEADER = {'X-Requested-With': 'dashboard-fetch'}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.op_conn = op_store.open_store(self.home)
        self.addCleanup(self.op_conn.db.close)
        self.dispatched = []
        dispatch = self.dispatched

        class FakeDispatcher:
            def dispatch_operator(self, *a, **k):
                dispatch.append(True)

        self.dispatcher = FakeDispatcher()
        self.trigger = wiring.make_manual_trigger(
            self.op_conn, cfg(), self.dispatcher, home=self.home)

    def serve_with(self, trigger='_default', **kw):
        if trigger == '_default':
            trigger = self.trigger
        handler = serve.create(None, now=lambda: 0.0,
                              on_operator_trigger=trigger,
                              on_operator_options=lambda: {'enabled': True},
                              **kw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def tear_down():
            # issue #216/#199: shutdown → join → server_close ordering, or a
            # closing socket crashes serve_forever's selector (WinError 10038)
            server.shutdown()
            thread.join(timeout=10)
            server.server_close()

        self.addCleanup(tear_down)
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

    def test_real_closure_receives_positional_contract(self):
        """The whole point of the MAJOR: a 4-positional-arg payload must reach
        the manual closure and fire a round (a single-arg closure 502s here)."""
        call = self.serve_with()
        status, body = call('POST', '/api/operator/trigger',
                            {'project': 'a__b', 'number': 7}, self.HEADER)
        self.assertEqual(200, status, body)
        self.assertTrue(body['ok'])
        self.assertTrue(body['id'].startswith('operator:'))
        self.assertEqual(len(self.dispatched), 1)
        row = self.op_conn.db.execute(
            "SELECT payload FROM work_items WHERE key LIKE 'op:manual:manual|%' "
            'ORDER BY created_at DESC LIMIT 1').fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row['payload'])['project'], 'a__b')
        self.assertEqual(json.loads(row['payload'])['number'], 7)

    def test_dialog_picks_reach_the_command_row(self):
        """Manual dialog agent/model must survive the whole path — POST → manual
        closure → payload → enqueue_command — or the operator round runs with
        the config default / DEFAULT_COMMAND (review MAJOR)."""
        call = self.serve_with()
        status, body = call('POST', '/api/operator/trigger',
                            {'project': 'a__b', 'number': 7,
                             'agent': 'opencode', 'model': 'kimi-for-coding/k3'},
                            self.HEADER)
        self.assertEqual(200, status, body)
        self.assertTrue(body['ok'])
        command = self.op_conn.pending_commands()[0]
        self.assertEqual(command['agent'], 'opencode')
        self.assertEqual(command['model'], 'kimi-for-coding/k3')

    def test_not_wired_503(self):
        call = self.serve_with(trigger=None)
        status, body = call('POST', '/api/operator/trigger',
                            {'project': 'a__b'}, self.HEADER)
        self.assertEqual(503, status)
        self.assertEqual('operator trigger not wired on this server', body['error'])

    def test_bad_shape_400_before_closure(self):
        call = self.serve_with()
        for bad, payload in (('number', {'project': 'a__b', 'number': -2}),
                             ('agent', {'project': 'a__b', 'agent': 3})):
            status, body = call('POST', '/api/operator/trigger', payload,
                                self.HEADER)
            self.assertEqual(400, status, bad)
            self.assertIn(bad, body['error'])

    def test_requires_header(self):
        call = self.serve_with()
        status, _ = call('POST', '/api/operator/trigger',
                         {'project': 'a__b'}, headers=None)
        self.assertEqual(403, status)


class OptionsTests(unittest.TestCase):
    def test_options_shape(self):
        options = wiring.make_options(cfg(), dispatcher=None)
        payload = options()
        self.assertTrue(payload['enabled'])
        self.assertIsNone(payload['default_agent'])
        self.assertEqual(payload['scope'], {'kind': 'serve-host'})
        self.assertIsInstance(payload['agents'], list)

    def test_options_carries_ok_envelope(self):
        """issue #301 defect 1: the frontend's openTriggerDialog operator
        branch rejects on ``!resp.ok || !data.ok`` — without the ok field the
        agent list never rendered (dialog hint showed 'HTTP 200'). The options
        response must carry the shared ok envelope."""
        options = wiring.make_options(cfg(), dispatcher=None)
        payload = options()
        self.assertTrue(payload['ok'])


class EventSinkTests(unittest.TestCase):
    def test_sink_filters_and_executor_reports(self):
        home = tempfile.mkdtemp(prefix='op-sink-')
        store = op_store.open_store(home)
        try:
            import queue
            feed = queue.Queue()
            sink = wiring.make_event_sink(cfg(), feed)
            sink('publication.sent', {'x': 1})
            self.assertTrue(feed.empty())
            sink('sync.failed', {'detail': 'boom'})
            kind, payload, _ = feed.get_nowait()
            self.assertEqual(kind, 'sync.failed')
        finally:
            store.db.close()


class OperatorConsumerTests(unittest.TestCase):
    def test_event_received_while_waiting_is_processed(self):
        """A blocking queue read must hand its event to the normal ingest gate."""
        import queue

        class StopConsumer(Exception):
            pass

        class Feed:
            def __init__(self):
                self.waits = 0

            def get_nowait(self):
                raise queue.Empty

            def get(self, timeout=None):
                self.waits += 1
                if self.waits == 1:
                    return ('sync.failed', {'entity': 'test'}, time.time())
                if self.waits == 2:
                    raise queue.Empty
                raise StopConsumer

        class InlineThread:
            def __init__(self, target, daemon):
                self.target = target

            def start(self):
                self.target()

        class Store:
            def __init__(self):
                self.sweeps = 0

            def sweep_commands(self):
                self.sweeps += 1

        class Dispatcher:
            def __init__(self):
                self.events = []

            def dispatch_operator(self):
                self.events.append('dispatch')

        feed = Feed()
        store = Store()
        dispatcher = Dispatcher()
        received = []

        def ingest(_store, _cfg, kind, payload):
            received.append((kind, payload))
            dispatcher.events.append('ingest:' + kind)
            return {'decision': 'fired'}

        with mock.patch.object(wiring.queue, 'Queue', return_value=feed), \
                mock.patch.object(wiring.threading, 'Thread', InlineThread), \
                mock.patch.object(wiring.op_ingest, 'ingest_event',
                                  side_effect=ingest), \
                mock.patch.object(wiring.op_store, 'recycle_wedged_rounds'), \
                mock.patch.object(wiring.op_store, 'drain_obsolete_readys'), \
                mock.patch.object(wiring.op_ingest, 'ingest_watchdog'):
            with self.assertRaises(StopConsumer):
                wiring.start_consumer(store, cfg(), dispatcher, 'unused')

        self.assertEqual(received, [('sync.failed', {'entity': 'test'})])
        self.assertEqual(dispatcher.events[:3],
                         ['dispatch', 'ingest:sync.failed', 'dispatch'])
        self.assertEqual(store.sweeps, 3)


class ExecutorTests(unittest.TestCase):
    def test_wal_checkpoint_truncates(self):
        """wal_checkpoint：真实 sqlite WAL 文件被 TRUNCATE 回收（issue #303）。"""
        home = tempfile.mkdtemp(prefix='op-wal-')
        db_path = Path(home) / 'dash.sqlite'
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('CREATE TABLE t (x INTEGER)')
            conn.execute('INSERT INTO t VALUES (1)')
            conn.commit()
            wal_files = [p for p in Path(home).iterdir() if p.name.endswith('-wal')]
            self.assertTrue(wal_files, 'WAL mode 未生成 -wal 文件')
            execute = wiring.make_executor(Path(home) / 'projects', None,
                                           refresh_fn=None, dash_db=str(db_path))
            receipt = execute('wal_checkpoint', '')
            self.assertTrue(receipt['ok'])
            self.assertIn('checkpointed', receipt)
        finally:
            conn.close()

    def test_wal_checkpoint_rejects_missing_db(self):
        """目标必须是真实存在的 sqlite 路径——拒绝编造路径（诚实失败）。"""
        execute = wiring.make_executor('unused', None, refresh_fn=None)
        with self.assertRaisesRegex(ValueError, 'OPERATOR_TARGET_INVALID'):
            execute('wal_checkpoint', 'C:/nope/nonexistent.sqlite')

    def test_command_resend_enqueues_and_dispatches(self):
        home = tempfile.mkdtemp(prefix='op-exec-')
        projects = Path(home) / 'projects'
        (projects / 'm__repo' / 'state').mkdir(parents=True)
        dispatched = []

        class FakeDispatcher:
            def dispatch(self, keys=None):
                dispatched.append(keys)
                return 1

        execute = wiring.make_executor(projects, FakeDispatcher(), refresh_fn=None)
        store = op_store.open_store(home)
        try:
            receipt = execute('command_resend', 'repair_round:617:m__repo')
            self.assertTrue(receipt['ok'])
            self.assertTrue(receipt['created'])
            self.assertEqual(dispatched, [['m__repo']])
            project = op_store.Store(projects / 'm__repo' / 'state')
            try:
                pending = project.pending_commands()
                self.assertEqual([c['action'] for c in pending], ['repair_round'])
                self.assertEqual(pending[0]['number'], 617)
            finally:
                project.db.close()
        finally:
            store.db.close()

    def test_command_resend_rejects_traversal_keys(self):
        """MAJOR-3: the resend target's third part builds `projects_root/key/state`
        — only the registry's org__repo shape may reach the filesystem."""
        home = tempfile.mkdtemp(prefix='op-exec3-')
        execute = wiring.make_executor(Path(home), None, refresh_fn=None)
        for target in ('repair_round:617:../../etc/passwd',
                       'repair_round:617:/abs/path',
                       'review_round:3:..',
                       'repair_round:617:',
                       'repair_round:617:org repo'):
            with self.assertRaises(ValueError) as caught:
                execute('command_resend', target)
            self.assertIn('OPERATOR_TARGET_INVALID', str(caught.exception))

    def test_unwired_verb_still_honest(self):
        """A verb outside the executor surface still fails loudly: with the
        gitea CLI absent the wired gitea verbs report OPERATOR_VERB_NOT_WIRED
        (they cannot run); with it present, a malformed target is refused as
        OPERATOR_TARGET_INVALID instead of guessing at a repo."""
        home = tempfile.mkdtemp(prefix='op-exec2-')
        execute = wiring.make_executor(Path(home), None, refresh_fn=None)
        want = ('OPERATOR_TARGET_INVALID' if shutil.which('gitea')
                else 'OPERATOR_VERB_NOT_WIRED')
        with self.assertRaisesRegex(ValueError, want):
            execute('gitea_create_issue', 'x')
        # A verb outside the surface entirely is always NOT_WIRED.
        with self.assertRaisesRegex(ValueError, 'OPERATOR_VERB_NOT_WIRED'):
            execute('no_such_verb', 'x')


class SinkInjectionTests(unittest.TestCase):
    def test_coordinator_attaches_sink_to_services(self):
        from codeloop.cp.server import Coordinator
        home = tempfile.mkdtemp(prefix='op-coord-')
        key = 'm__repo'
        cfg_dir = Path(home) / key
        cfg_dir.mkdir(parents=True)
        (cfg_dir / 'control-plane.json').write_text(json.dumps(
            {'platform': {'url': 'https://gitea.invalid', 'repo': 'm/repo'},
             'remote': True}), encoding='utf-8')
        seen = []

        def factory(k, cfg, auth):
            store = op_store.open_store(home)
            seen.append(store)
            return type('S', (), {'store': store})()

        coord = Coordinator(home, str(Path(home) / 'registry.sqlite'),
                            service_factory=factory)
        coord.event_sink = lambda kind, payload: seen.append(kind)
        service = coord.service_for(key)
        service.store.event('sync.failed', {})
        self.assertIn('sync.failed', seen)
        coord = Coordinator(home, str(Path(home) / 'registry.sqlite'),
                            service_factory=factory)
        coord.event_sink = lambda kind, payload: seen.append(kind)
        service = coord.service_for(key)
        service.store.event('sync.failed', {})
        self.assertIn('sync.failed', seen)
        for store in seen:
            if hasattr(store, 'db'):
                store.db.close()


class OperatorActCliTests(unittest.TestCase):
    """scripts/operator.py act: local verbs run in-process against the store;
    wired verbs forward to the serve data plane (review MAJOR-3)."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        path = Path(__file__).resolve().parents[1] / 'scripts' / 'operator.py'
        spec = importlib.util.spec_from_file_location('op_cli', path)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def _store_and_claim(self):
        home = tempfile.mkdtemp(prefix='op-cli-')
        store = op_store.open_store(home)
        item, _ = op_store.put_signal(store, source='alert', kind='loop_stall',
                                      key='k1', payload={'message': 'stall'},
                                      org_repo='mocx__skills')
        claim = face.claim(store, worker='HOST/operator', ttl=1_800,
                           item_id=item['id'])
        return home, store, claim

    def test_local_verb_acts_in_process(self):
        import io
        from contextlib import redirect_stdout
        home, store, claim = self._store_and_claim()
        try:
            namespace = type('A', (), {'verb': 'no_op', 'execution': claim['id'],
                                       'token': claim['token'], 'target': '',
                                       'detail': ''})()
            opened = []
            original = op_store.open_store

            def fake_open(home=None, check_same_thread=False):
                opened.append(home)
                return store

            op_store.open_store = fake_open
            try:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = self.mod.cmd_act(namespace)
            finally:
                op_store.open_store = original
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue())['ok'], True)
            # cmd_act closed the in-process store; re-open on the same home to
            # assert the audited verb landed in the round's events.
            peek = op_store.open_store(home)
            try:
                events = face._act_events(peek, claim['id'])
            finally:
                peek.db.close()
            self.assertEqual([e['verb'] for e in events], ['no_op'])
        finally:
            store.db.close()

    def test_wired_verb_forwards_to_data_plane(self):
        import io
        from contextlib import redirect_stdout
        seen = {}

        def fake_plane(verb, assignment=None, config=None):
            seen.update(verb=verb, assignment=assignment)
            return {'ok': True, 'verb': verb, 'receipt': {'ok': True}}

        namespace = type('A', (), {'verb': 'collect_refresh', 'execution': 'e1',
                                   'token': 7, 'target': 't', 'detail': ''})()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.mod.cmd_act(namespace, data_plane=fake_plane)
        self.assertEqual(rc, 0)
        self.assertEqual(seen['verb'], 'collect_refresh')
        self.assertEqual(seen['assignment']['token'], 7)
        self.assertEqual(json.loads(buf.getvalue())['ok'], True)


class OperatorContractDocTests(unittest.TestCase):
    """issue #303 分级升级契约锁在规则文档（源码级回归，改契约必先改文档）。"""

    def test_escalation_ladder_contract_documented(self):
        doc_path = Path(__file__).resolve().parents[1] / 'references' / 'operator.md'
        if not doc_path.is_file():
            doc_path = (Path(__file__).resolve().parents[3]
                        / 'skills' / 'code-loop' / 'references' / 'operator.md')
        doc = doc_path.read_text(encoding='utf-8')
        self.assertTrue('gitea_create_issue' in doc or 'github_create_issue' in doc)
        self.assertIn('必须自动落 issue', doc or '')
        self.assertIn('wal_checkpoint', doc)
        self.assertIn('升级分级契约', doc)

    def test_wal_checkpoint_verb_declared_and_wired(self):
        self.assertIn('wal_checkpoint', face.OPERATOR_VERBS)
        wiring_src = (Path(__file__).resolve().parents[1]
                      / 'codeloop' / 'operator' / 'wiring.py').read_text(
            encoding='utf-8')
        self.assertIn("'wal_checkpoint'", wiring_src)
        self.assertIn("wal_checkpoint(TRUNCATE)", wiring_src)


class GiteaVerbWiringTests(unittest.TestCase):
    """#303 PR4：gitea 系动词必须在 serve 装配期接线——PR#304 落了分级升级
    契约和 executor，但 wire 集合一直没含这三个动词，operator 按契约尝试
    gitea_create_issue 只会拿到 OPERATOR_VERB_NOT_WIRED（10-15 10:31 轮日志
    实证），升级死在台账。源码绊线锁装配行，face 级锁可执行性。"""

    def test_serve_wires_gitea_verbs(self):
        serve_src = (Path(__file__).resolve().parents[1]
                     / 'scripts' / 'cp_server.py').read_text(encoding='utf-8')
        flat = ' '.join(serve_src.split())   # 折行归一，只锁集合内容
        for v in ('collect_refresh', 'command_resend', 'wal_checkpoint',
                  'gitea_create_issue', 'gitea_comment_issue',
                  'github_create_issue', 'github_comment_issue', 'escalate_human'):
            self.assertIn("'%s'" % v, flat)

    def test_doc_marks_gitea_verbs_available(self):
        doc_path = Path(__file__).resolve().parents[1] / 'references' / 'operator.md'
        if not doc_path.is_file():
            doc_path = (Path(__file__).resolve().parents[3]
                        / 'skills' / 'code-loop' / 'references' / 'operator.md')
        doc = doc_path.read_text(encoding='utf-8')
        self.assertNotIn('PR3 声明，PR4 wire', doc)

    def test_wired_gitea_verb_executes_and_audits(self):
        saved_wired = set(face.WIRED)
        saved_executor = face._EXECUTOR
        seen = {}

        def fake_executor(verb, target, detail=''):
            seen.update(verb=verb, target=target)
            return {'ok': True}

        face.wire({'gitea_create_issue'}, fake_executor)
        try:
            home = tempfile.mkdtemp(prefix='op-gitea-')
            store = op_store.open_store(home)
            try:
                item, _ = op_store.put_signal(store, source='alert',
                                              kind='loop_stall', key='k1',
                                              payload={}, org_repo='m')
                claim = face.claim(store, worker='H/operator', ttl=1_800,
                                   item_id=item['id'])
                receipt = face.act(store, claim['id'], claim['token'],
                                   'gitea_create_issue',
                                   target='mocx__skills:[BUG] x',
                                   detail='body')
                self.assertTrue(receipt['ok'])
                self.assertEqual(seen['verb'], 'gitea_create_issue')
                self.assertEqual(seen['target'], 'mocx__skills:[BUG] x')
                events = face._act_events(store, claim['id'])
                self.assertEqual([e['verb'] for e in events],
                                 ['gitea_create_issue'])
            finally:
                store.db.close()
        finally:
            face.WIRED.clear()
            face.WIRED.update(saved_wired)
            face._EXECUTOR = saved_executor

    def test_wired_github_verb_executes_and_audits(self):
        saved_wired = set(face.WIRED)
        saved_executor = face._EXECUTOR
        seen = {}

        def fake_executor(verb, target, detail=''):
            seen.update(verb=verb, target=target)
            return {'ok': True}

        face.wire({'github_create_issue'}, fake_executor)
        try:
            home = tempfile.mkdtemp(prefix='op-gh-')
            store = op_store.open_store(home)
            try:
                item, _ = op_store.put_signal(store, source='alert',
                                              kind='loop_stall', key='k1',
                                              payload={}, org_repo='m')
                claim = face.claim(store, worker='H/operator', ttl=1_800,
                                   item_id=item['id'])
                receipt = face.act(store, claim['id'], claim['token'],
                                   'github_create_issue',
                                   target='mocx__skills:[BUG] x',
                                   detail='body')
                self.assertTrue(receipt['ok'])
                self.assertEqual(seen['verb'], 'github_create_issue')
                self.assertEqual(seen['target'], 'mocx__skills:[BUG] x')
                events = face._act_events(store, claim['id'])
                self.assertEqual([e['verb'] for e in events],
                                 ['github_create_issue'])
            finally:
                store.db.close()
        finally:
            face.WIRED.clear()
            face.WIRED.update(saved_wired)
            face._EXECUTOR = saved_executor


if __name__ == '__main__':
    unittest.main()
