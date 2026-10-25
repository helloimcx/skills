#!/usr/bin/env python3
"""CP server over loopback HTTP: auth matrix, machine stamping, full protocol.
Platform is faked per project; worktree facts are plain data (no git here)."""
import copy
import gc
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.cp.server import Coordinator, make_server
from codeloop.cp.service import Service

HEADER = {'Content-Type': 'application/json', 'X-CP-Protocol': '1'}


class Platform:
    def __init__(self):
        self.data = {'base_sha': 'base1', 'issues': [], 'prs': []}
        self.writes = []

    def snapshot(self, known=None):
        return copy.deepcopy(self.data)

    def raw_file(self, path, ref):
        return None

    def pr_files(self, number):
        return []
    def find(self, marker, action, payload):
        return next((r for a, p, r in self.writes if marker in p.get('body', '')), None)

    def write(self, action, payload):
        receipt = {'number': 100 + len(self.writes)}
        self.writes.append((action, payload, receipt))
        return receipt


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # One real project key per case: without a private data home the shared
        # observer store would serve another case's facts (same key 'a__b').
        self._home = os.environ.get('CODE_LOOP_HOME')
        os.environ['CODE_LOOP_HOME'] = self.tmp.name
        self.platforms = {}
        self._services = []
        registry_path = self.root / 'registry.sqlite'
        self.registry = self.registry_ref(registry_path)
        self.coordinator = Coordinator(self.root / 'projects', registry_path,
                                       service_factory=self.factory)
        self.httpd = make_server(self.coordinator, '127.0.0.1', 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        # Project a__b is exposed; b__c exists but is not remote.
        self.write_config('a__b', remote=True)
        self.write_config('b__c', remote=False)
        self.code = self.registry.create_enroll_code('dev-b', ['a__b'])
        out = self.request('POST', '/api/v1/enroll', {'code': self.code})[1]
        self.token = out['result']['token']

    def registry_ref(self, path):
        from codeloop.cp.registry import Registry
        self.registry = Registry(str(path))
        return self.registry

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        self.registry.close()
        # Per-request Service stores are closed inside the handler thread; on
        # Windows the lingering file handle releases once GC finalizes them.
        gc.collect()
        self.tmp.cleanup()
        if self._home is None:
            os.environ.pop('CODE_LOOP_HOME', None)
        else:
            os.environ['CODE_LOOP_HOME'] = self._home

    def factory(self, key, cfg, authorization):
        service = Service(cfg, self.platforms.setdefault(key, Platform()),
                          authorization=authorization)
        self._services.append(service)
        return service

    def write_config(self, key, remote):
        cfg_dir = self.root / 'projects' / key
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg = {'platform': {'url': 'https://gitea.invalid', 'repo': key.replace('__', '/')},
               'remote': remote}
        (cfg_dir / 'control-plane.json').write_text(json.dumps(cfg), encoding='utf-8')

    def request(self, method, path, body=None, token=None, protocol='1', raw=None):
        url = 'http://127.0.0.1:%d%s' % (self.port, path)
        headers = dict(HEADER)
        headers['X-CP-Protocol'] = protocol
        if token:
            headers['Authorization'] = 'Bearer ' + token
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode('utf-8'))
        except (urllib.error.URLError, OSError):
            return 413, {'code': 'PAYLOAD_TOO_LARGE'}

    def issue(self):
        return {'number': 1, 'state': 'open', 'title': '[Content] bug', 'body': 'evidence',
                'labels': [], 'comments': []}

    def test_observer_crawl_factory_loads_server_side_config(self):
        # The driver builds crawlers from server-side configs: no working
        # directory, no git checkout, no policy cache. A loopback platform
        # needs no token, so this stays credential-free.
        cfg_dir = self.root / 'projects' / 'loop__back'
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / 'control-plane.json').write_text(json.dumps({
            'platform': {'url': 'http://127.0.0.1:1', 'repo': 'loop/back'}}), encoding='utf-8')
        import cp_server
        crawl = cp_server._observer_crawl_factory(cfg_dir / 'control-plane.json')
        self.assertTrue(callable(crawl))

    def test_health_needs_no_auth(self):
        status, env = self.request('GET', '/health')
        self.assertEqual(status, 200)
        self.assertTrue(env['ok'])
        self.assertEqual(env['result']['protocol_version'], 1)

    def test_worker_task_report_is_bound_to_the_claimed_machine(self):
        machine = self.registry.authenticate(self.token)
        task, _ = self.registry.worker_tasks.enqueue(
            'worker.project_diagnose', target_machine_id=machine['id'],
            project='a__b', payload={'loop': 'discover'},
            source_event_id='stall:a__b:discover:1', now=1000.0)
        self.registry.worker_tasks.mark_dispatched(task['id'], machine['id'], now=1001.0)
        body = {'task': task['id'], 'state': 'RUNNING', 'session_id': 'sess-1'}
        status, env = self.request('POST', '/api/v1/worker-tasks/report',
                                   body, token=self.token)
        self.assertEqual(200, status)
        self.assertTrue(env['ok'])
        self.assertEqual(('RUNNING', 'sess-1'),
                         (env['result']['state'], env['result']['session_id']))

        other_code = self.registry.create_enroll_code('dev-c', [])
        other = self.request('POST', '/api/v1/enroll',
                             {'code': other_code})[1]['result']
        status, env = self.request(
            'POST', '/api/v1/worker-tasks/report',
            {'task': task['id'], 'state': 'SUCCEEDED',
             'result': {'summary': 'spoof'}}, token=other['token'])
        self.assertEqual(409, status)
        self.assertEqual('TASK_NOT_OWNED', env['code'])
        self.assertEqual('RUNNING', self.registry.worker_tasks.get(task['id'])['state'])

        status, env = self.request(
            'POST', '/api/v1/worker-tasks/report',
            {'task': task['id'], 'state': 'SUCCEEDED',
             'result': {'summary': 'checked', 'actions': []}}, token=self.token)
        self.assertEqual(200, status)
        self.assertEqual('SUCCEEDED', env['result']['state'])
        self.assertEqual({'summary': 'checked', 'actions': []},
                         env['result']['result'])

    def test_auth_matrix(self):
        status, env = self.request('POST', '/api/projects/a__b/sync', {})
        self.assertEqual(status, 401)
        self.assertEqual(env['code'], 'UNAUTHORIZED')
        status, env = self.request('POST', '/api/projects/a__b/sync', {}, token='forged')
        self.assertEqual(status, 401)
        status, env = self.request('POST', '/api/projects/a__b/sync', {}, token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(env['ok'])
        # Disabled: writes 403, reads pass.
        self.registry.set_enabled('dev-b', False)
        status, env = self.request('POST', '/api/projects/a__b/claim',
                                   {'loop': 'discover', 'worker': 'w'}, token=self.token)
        self.assertEqual(status, 403)
        self.assertEqual(env['code'], 'MACHINE_DISABLED')
        status, env = self.request('GET', '/api/projects/a__b/status', token=self.token)
        self.assertEqual(status, 200)
        # Revoked: everything 401.
        self.registry.revoke('dev-b')
        status, env = self.request('GET', '/api/projects/a__b/status', token=self.token)
        self.assertEqual(status, 401)

    def test_project_isolation(self):
        status, env = self.request('POST', '/api/projects/b__c/sync', {}, token=self.token)
        self.assertEqual(status, 404)
        self.assertEqual(env['code'], 'PROJECT_NOT_REMOTE')
        status, env = self.request('POST', '/api/projects/none__x/sync', {}, token=self.token)
        self.assertEqual(status, 404)
        self.assertEqual(env['code'], 'PROJECT_NOT_FOUND')
        # A second machine without a grant reads fine but cannot claim.
        other_code = self.registry.create_enroll_code('dev-c', [])
        other = self.request('POST', '/api/v1/enroll', {'code': other_code})[1]['result']
        status, env = self.request('GET', '/api/projects/a__b/status', token=other['token'])
        self.assertEqual(status, 200)
        status, env = self.request('POST', '/api/projects/a__b/claim',
                                   {'loop': 'discover', 'worker': 'w'}, token=other['token'])
        self.assertEqual(status, 403)
        self.assertEqual(env['code'], 'PROJECT_NOT_GRANTED')

    def test_claim_without_worker_is_the_bare_machine_name(self):
        """No suffix appends no label: a direct HTTP client that omits the
        worker gets the enrolled machine name, not the old fallback's
        ambiguous '<machine><machine>'."""
        self.platforms.setdefault('a__b', Platform()).data['issues'] = [self.issue()]
        self.request('POST', '/api/projects/a__b/sync', {}, token=self.token)
        status, env = self.request('POST', '/api/projects/a__b/claim',
                                   {'loop': 'discover'}, token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(env['result']['execution']['worker'], 'dev-b')

    def test_full_lifecycle_with_stamping_and_policy(self):
        self.platforms.setdefault('a__b', Platform()).data['issues'] = [self.issue()]
        status, env = self.request('POST', '/api/projects/a__b/sync', {}, token=self.token)
        self.assertTrue(env['ok'])
        status, env = self.request('POST', '/api/projects/a__b/claim',
                                   {'loop': 'discover', 'worker': 'inspector'}, token=self.token)
        self.assertEqual(status, 200)
        result = env['result']
        self.assertEqual(result['execution']['worker'], 'dev-b/inspector')
        self.assertEqual(result['item']['kind'], 'discover')
        self.assertEqual(result['policy']['base'], 'main')
        execution, item = result['execution'], result['item']
        body = {'execution': execution['id'], 'token': execution['token'],
                'result': {'decision': 'CONFIRMED', 'reviewer': 'server-side-verifier',
                           'summary': 'proof', 'action': 'create_issue',
                           'payload': {'title': '[Content] bug', 'body': 'evidence'}}}
        status, env = self.request('POST', '/api/projects/a__b/report', body, token=self.token)
        self.assertTrue(env['ok'])
        status, env = self.request('POST', '/api/projects/a__b/publish/begin',
                                   {'execution': execution['id'], 'token': execution['token']},
                                   token=self.token)
        self.assertTrue(env['ok'])
        prepared = env['result']
        self.assertFalse(prepared['push_required'])
        self.assertIsNone(prepared['receipt'])
        status, env = self.request('POST', '/api/projects/a__b/publish/commit',
                                   {'execution': execution['id'], 'token': execution['token'],
                                    'op': prepared['op']}, token=self.token)
        self.assertTrue(env['ok'])
        self.assertEqual(env['result']['number'], 100)
        self.assertEqual(self.platforms['a__b'].writes[-1][1]['body'],
                         '> 🤖 本条由 code-loop **discover** 循环自动生成\n\nevidence'
                         + '\n\n<!-- code-loop:%s -->' % prepared['op']
                         + '\n<!-- code-loop-agent: discover -->')
        # publish/commit derived the terminal state (D2): no finish verb.
        store = self.coordinator.service_for('a__b').store
        state = store.db.execute('SELECT state FROM executions WHERE id=?',
                                 (execution['id'],)).fetchone()['state']
        store.db.close()
        self.assertEqual('SUCCEEDED', state)

    def test_bind_verify_endpoints_are_data_only(self):
        self.platforms.setdefault('a__b', Platform()).data['prs'] = [
            {'number': 2, 'state': 'open', 'title': 'fix', 'body': 'Closes #1',
             'head': {'sha': 'head1', 'ref': 'fix/mocx/bug'},
             'base': {'sha': 'base1', 'ref': 'main'}, 'merged': False,
             'mergeable': True, 'draft': False, 'comments': [], 'reviews': [], 'checks': []}]
        self.request('POST', '/api/projects/a__b/sync', {}, token=self.token)
        _, env = self.request('POST', '/api/projects/a__b/claim',
                              {'loop': 'reviewer', 'worker': 'w'}, token=self.token)
        execution = env['result']['execution']
        context = {'worktree': 'D:/wt/task', 'branch': 'fix/mocx/bug', 'head': 'wrong'}
        status, env = self.request('POST', '/api/projects/a__b/bind',
                                   {'execution': execution['id'], 'token': execution['token'],
                                    'context': context}, token=self.token)
        self.assertEqual(status, 200)
        self.assertFalse(env['ok'])
        self.assertIn('PR_HEAD_MISMATCH', env['message'])
        context['head'] = 'head1'
        status, env = self.request('POST', '/api/projects/a__b/bind',
                                   {'execution': execution['id'], 'token': execution['token'],
                                    'context': context}, token=self.token)
        self.assertTrue(env['ok'])
        checks = {'passed': True, 'unchanged': True, 'head': 'head1', 'checks': [],
                  'env_signature': None}
        status, env = self.request('POST', '/api/projects/a__b/verify',
                                   {'execution': execution['id'], 'token': execution['token'],
                                    'context': context, 'checks': checks}, token=self.token)
        self.assertTrue(env['ok'])
        self.assertTrue(env['result']['input_version'])

    def test_malformed_requests_rejected(self):
        status, env = self.request('POST', '/api/projects/a__b/sync', None, token=self.token,
                                   raw=b'not json')
        self.assertEqual(status, 400)
        status, env = self.request('POST', '/api/projects/a__b/sync', {}, token=self.token,
                                   protocol='9')
        self.assertEqual(status, 400)
        self.assertEqual(env['code'], 'PROTOCOL_UNSUPPORTED')
        status, env = self.request('POST', '/api/projects/a__b/sync', {}, token=self.token,
                                   raw=b'x' * (2 * 1024 * 1024 + 1))
        self.assertEqual(status, 413)

    def test_late_writes_succeed_and_retired_verbs_404(self):
        self.platforms.setdefault('a__b', Platform()).data['issues'] = [self.issue()]
        self.request('POST', '/api/projects/a__b/sync', {}, token=self.token)
        _, env = self.request('POST', '/api/projects/a__b/claim',
                              {'loop': 'discover', 'worker': 'w'}, token=self.token)
        execution = env['result']['execution']
        # The clock no longer fences writes: an unstealable-but-slow worker's
        # late report succeeds and derives the terminal state (no-action
        # discover REJECTED reports end at report, D1).
        import time as _t
        past = _t.time() - 7200
        store = self.coordinator.service_for('a__b').store
        store.db.execute('UPDATE executions SET expires_at=? WHERE id=?', (past, execution['id']))
        store.db.commit()
        store.db.close()
        status, env = self.request('POST', '/api/projects/a__b/report',
                                   {'execution': execution['id'], 'token': execution['token'],
                                    'result': {'decision': 'REJECTED', 'reviewer': 'v',
                                               'summary': 'late but delivered'}}, token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(env['ok'])
        self.assertEqual(env['result']['execution_state'], 'SUCCEEDED')
        store = self.coordinator.service_for('a__b').store
        state = store.db.execute('SELECT state FROM executions WHERE id=?',
                                 (execution['id'],)).fetchone()['state']
        store.db.close()
        self.assertEqual('SUCCEEDED', state)
        # Retired verbs are gone from the HTTP surface.
        for verb in ('finish', 'heartbeat', 'recover'):
            status, env = self.request('POST', '/api/projects/a__b/' + verb,
                                       {'execution': execution['id'], 'token': 1},
                                       token=self.token)
            self.assertEqual(404, status, verb)


class JoinRouteTests(ServerTests):
    """Wire surface of worker-initiated pairing: join/poll are unauthenticated
    (like /enroll), approve lives behind the server management CLI, consume is
    protected by the pairing secret. Cert fingerprint compare is exercised by
    handing the server's own cert fingerprint into approve."""

    def test_join_poll_consume_roundtrip(self):
        import hashlib
        self.write_config('a__b', remote=True)
        secret = 'the-pairing-secret'
        self.registry.create_join('gpu-box', hashlib.sha256(secret.encode()).hexdigest(),
                                  cert_fp='deadbeef', agent_version='1')
        # 等待批准：无 secret 的 poll 只读状态
        status, env = self.request('POST', '/api/v1/join/poll',
                                   {'name': 'gpu-box'})
        self.assertEqual(200, status)
        self.assertEqual('PENDING', env['result']['state'])
        self.registry.approve_join('gpu-box', ['a__b'], server_cert_fp='deadbeef')
        # 批准后：错误密钥拿不到 token（失败信封，不泄露明文）
        status, env = self.request('POST', '/api/v1/join/poll',
                                   {'name': 'gpu-box', 'secret': 'wrong'})
        self.assertEqual(200, status)
        self.assertFalse(env['ok'])
        self.assertIn('JOIN_SECRET_MISMATCH', env['code'])
        # 正确密钥拿到 token
        status, env = self.request('POST', '/api/v1/join/poll',
                                   {'name': 'gpu-box', 'secret': secret})
        self.assertEqual(200, status)
        self.assertTrue(env['ok'])
        self.assertEqual('CONSUMED', env['result']['state'])
        self.assertTrue(env['result']['token'])
        machine = self.registry.authenticate(env['result']['token'])
        self.assertIsNotNone(machine)
        self.assertEqual('gpu-box', machine['name'])


class GrantRequestRouteTests(ServerTests):
    """POST /api/v1/requests is token-authenticated; the server stamps the
    machine from the token — the wire format has no machine field to forge.
    Approval (writing grants) stays a server-local operator action."""

    def test_request_route_stamps_machine_and_is_idempotent(self):
        status, env = self.request('POST', '/api/v1/requests', {'project': 'b__c'},
                                   token=self.token)
        self.assertEqual(200, status)
        self.assertEqual('PENDING', env['result']['state'])
        first = env['result']['id']
        status, env = self.request('POST', '/api/v1/requests', {'project': 'b__c'},
                                   token=self.token)
        self.assertEqual(first, env['result']['id'])
        rows = self.registry.list_grant_requests()
        self.assertEqual(1, len(rows))
        self.assertEqual('dev-b', rows[0]['machine_name'])
        self.registry.approve_grant_request(first)
        status, env = self.request('POST', '/api/v1/requests', {'project': 'b__c'},
                                   token=self.token)
        self.assertEqual('ALREADY_GRANTED', env['result']['state'])
        machine_id = self.registry.authenticate(self.token)['id']
        self.assertTrue(self.registry.has_grant(machine_id, 'b__c'))

    def test_request_route_requires_token(self):
        status, env = self.request('POST', '/api/v1/requests', {'project': 'b__c'})
        self.assertEqual(401, status)
        status, env = self.request('POST', '/api/v1/requests', {'project': 'b__c'},
                                   token='forged')
        self.assertEqual(401, status)

    def test_request_route_requires_project(self):
        status, env = self.request('POST', '/api/v1/requests', {}, token=self.token)
        self.assertEqual(200, status)
        self.assertFalse(env['ok'])
        self.assertIn('REQUEST_PROJECT_REQUIRED', env['code'])

    def test_request_route_revoked_token_is_401(self):
        # 平台约定：revoked 即刻 401（disabled 才是写 403）——新路由走同一 _machine 门
        self.registry.revoke('dev-b')
        status, env = self.request('POST', '/api/v1/requests', {'project': 'b__c'},
                                   token=self.token)
        self.assertEqual(401, status)


class PairingClientE2ETests(ServerTests):
    """enroll_pairing / enroll_machine against the real in-process server:
    the machine-global config they write must name only the server URL —
    never an automatic project pin (registration ≠ authorization)."""

    def test_pairing_enroll_writes_config_without_project_pin(self):
        from unittest import mock
        from codeloop.cp.client import enroll_pairing
        config_path = self.root / 'worker-global.json'
        token_dir = self.root / 'credentials'
        out = {}

        def run():
            try:
                out['value'] = enroll_pairing(
                    'http://127.0.0.1:%d' % self.port, 'pair-worker',
                    config_path=str(config_path), poll_seconds=0.05,
                    poll_timeout=10)
            except Exception as exc:  # noqa: BLE001 - surface in the assertion
                out['error'] = exc

        with mock.patch('codeloop.cp.client.credentials_dir', return_value=token_dir):
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            deadline = time.time() + 10
            while time.time() < deadline and \
                    'pair-worker' not in [r['name'] for r in self.registry.list_joins()]:
                time.sleep(0.05)
            self.registry.approve_join('pair-worker', [], server_cert_fp=None)
            worker.join(timeout=10)
        self.assertNotIn('error', out, str(out.get('error')))
        cfg = json.loads(config_path.read_text(encoding='utf-8'))
        self.assertEqual({'url': 'http://127.0.0.1:%d' % self.port}, cfg['server'])
        self.assertNotIn('project', cfg['server'])
        self.assertTrue(token_dir.is_dir())

    def test_code_enroll_writes_config_without_project_pin(self):
        from unittest import mock
        from codeloop.cp.client import enroll_machine
        code = self.registry.create_enroll_code('dev-e', ['a__b'])
        config_path = self.root / 'worker-global-code.json'
        with mock.patch('codeloop.cp.client.credentials_dir',
                        return_value=self.root / 'credentials2'):
            enroll_machine('http://127.0.0.1:%d' % self.port, code,
                           config_path=str(config_path))
        cfg = json.loads(config_path.read_text(encoding='utf-8'))
        self.assertEqual({'url': 'http://127.0.0.1:%d' % self.port}, cfg['server'])
        self.assertNotIn('project', cfg['server'])


class SettingsTests(unittest.TestCase):
    def test_observer_period_must_be_positive(self):
        import cp_server
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            for bad in (0, -1, 'soon', True):
                (home / 'server.json').write_text(json.dumps({'observer_period': bad}),
                                                  encoding='utf-8')
                with self.assertRaises(SystemExit):
                    cp_server.load_settings(home)
            (home / 'server.json').write_text(json.dumps({'observer_period': 30}), encoding='utf-8')
            self.assertEqual(30, cp_server.load_settings(home)['observer_period'])
            (home / 'server.json').write_text('{}', encoding='utf-8')
            self.assertEqual(cp_server.DEFAULT_OBSERVER_PERIOD,
                             cp_server.load_settings(home)['observer_period'])


class CliTests(unittest.TestCase):
    def test_init_enroll_machines_roundtrip(self):
        repo = Path(__file__).resolve().parents[3]
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'cp_server.py'
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / 'server'
            def run(*args):
                return subprocess.run([sys.executable, str(script), '--home', str(home)] + list(args),
                                      capture_output=True, text=True, cwd=str(repo))
            self.assertEqual(run('init').returncode, 0)
            self.assertTrue((home / 'server.json').is_file())
            self.assertTrue((home / 'registry.sqlite').is_file())
            made = run('enroll-code', '--name', 'dev-b', '--project', 'a__b')
            self.assertEqual(made.returncode, 0, made.stderr)
            code = json.loads(made.stdout)['result']['code']
            self.assertTrue(code)
            listed = run('machines', 'list')
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(json.loads(listed.stdout)['result']['machines'], [])

    def test_serve_recovers_stale_daemon_lock_and_still_refuses_double_start(self):
        """进程死亡残留的 mkdir 锁 + 端口空闲 → serve 自动恢复启动（机器重启/
        崩溃后自启不再被锁挡住）；端口仍有活服务应答 → 真双开，维持拒绝。"""
        import socket
        import urllib.request
        repo = Path(__file__).resolve().parents[3]
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'cp_server.py'
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / 'server'
            probe = socket.socket()
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
            probe.close()

            def run(*args):
                return subprocess.run([sys.executable, str(script), '--home', str(home)]
                                      + list(args), capture_output=True, text=True,
                                      cwd=str(repo))

            self.assertEqual(run('init').returncode, 0)
            # Keep the serve process off the real machine: one unreachable
            # project gives the observer driver something to tick on (its store
            # appearing under the redirected data home proves the driver thread
            # was really started), and the data home redirect keeps even that
            # store out of ~/.code-loop.
            settings = json.loads((home / 'server.json').read_text(encoding='utf-8'))
            projects = Path(temp) / 'projects'
            cfg_dir = projects / 'a__b'
            cfg_dir.mkdir(parents=True)
            (cfg_dir / 'control-plane.json').write_text(json.dumps({
                'platform': {'url': 'http://127.0.0.1:1', 'repo': 'a/b', 'timeout': 1},
                'remote': True,
            }), encoding='utf-8')
            settings['projects_root'] = str(projects)
            # Tests must never claim the operator UI port: a temp home inherits
            # the production default 8712, and any moment that port is free a
            # test server binds it and then squats the dashboard (issue #249).
            settings['ui_port'] = None
            (home / 'server.json').write_text(json.dumps(settings), encoding='utf-8')
            (home / 'daemon.lock').mkdir()  # 模拟进程死亡残留
            data_home = Path(temp) / 'data'
            env = dict(os.environ, CODE_LOOP_HOME=str(data_home))

            serve = subprocess.Popen(
                [sys.executable, str(script), '--home', str(home), 'serve',
                 '--port', str(port), '--exit-on-stdin-eof'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=str(repo), env=env)
            try:
                up = False
                deadline = time.time() + 15
                while time.time() < deadline:
                    try:
                        with urllib.request.urlopen(
                                'http://127.0.0.1:%d/health' % port, timeout=2) as resp:
                            up = resp.status == 200
                        break
                    except Exception:
                        time.sleep(0.3)
                self.assertTrue(up, 'serve did not recover from stale lock')
                # The observer driver thread really started: it created the
                # shared store (a dead import inside that thread leaves the
                # store absent — the 2026-09-17 cp_server regression).
                store = data_home / 'observer.sqlite'
                deadline = time.time() + 10
                while not store.exists() and time.time() < deadline:
                    time.sleep(0.2)
                self.assertTrue(store.exists(), 'serve must start the observer driver')
                self.assertTrue((home / 'daemon.lock').is_file(),
                                'legacy dir must be migrated to the lock file')
                # 真双开：锁在、端口有活服务 → 拒绝，且锁不被清（二次启动不变量）
                again = run('serve', '--port', str(port))
                self.assertNotEqual(0, again.returncode)
                self.assertIn('DAEMON_LOCK_EXISTS', again.stdout + again.stderr)
                self.assertTrue((home / 'daemon.lock').is_file(),
                                'refusal must not delete the lock')
            finally:
                serve.terminate()
                try:
                    serve.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    serve.kill()

    def test_concurrent_start_with_stale_lock_has_single_winner(self):
        """并发启动撞陈旧锁：原子 rename 声明恢复 ⇒ 恰一个赢家起来服务，
        其余响亮退出——Windows 双绑允许下不得出现双 daemon。"""
        import socket
        import urllib.request
        repo = Path(__file__).resolve().parents[3]
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'cp_server.py'
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / 'server'
            probe = socket.socket()
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
            probe.close()

            def run(*args):
                return subprocess.run([sys.executable, str(script), '--home', str(home)]
                                      + list(args), capture_output=True, text=True,
                                      cwd=str(repo))

            self.assertEqual(run('init').returncode, 0)
            # Keep every starter off the real machine: empty projects root for
            # the observer driver, redirected data home for its store.
            settings = json.loads((home / 'server.json').read_text(encoding='utf-8'))
            settings['projects_root'] = str(Path(temp) / 'projects')
            settings['ui_port'] = None  # 绝不占看板端口（issue #249）
            (home / 'server.json').write_text(json.dumps(settings), encoding='utf-8')
            env = dict(os.environ, CODE_LOOP_HOME=str(Path(temp) / 'data'))
            (home / 'daemon.lock').mkdir()

            starters = [subprocess.Popen(
                [sys.executable, str(script), '--home', str(home), 'serve',
                 '--port', str(port), '--exit-on-stdin-eof'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=str(repo), env=env) for _ in range(3)]
            up = False
            deadline = time.time() + 20
            while time.time() < deadline:
                alive = [s for s in starters if s.poll() is None]
                if len(alive) <= 1:
                    try:
                        with urllib.request.urlopen(
                                'http://127.0.0.1:%d/health' % port, timeout=2) as resp:
                            up = resp.status == 200
                        break
                    except Exception:
                        pass
                time.sleep(0.3)
            losers = [s for s in starters if s.poll() is not None]
            for s in losers:
                out = s.stdout.read() if s.stdout else ''
                self.assertNotEqual(0, s.returncode, out[:200])
                self.assertIn('DAEMON_LOCK', out, out[:200])
            try:
                self.assertTrue(up, 'no single winner came up')
                self.assertEqual(1, sum(1 for s in starters if s.poll() is None),
                                 'more than one serve survived')
                self.assertTrue((home / 'daemon.lock').is_file())
            finally:
                for s in starters:
                    if s.poll() is None:
                        s.terminate()
                for s in starters:
                    try:
                        s.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        s.kill()

    def test_joins_zero_project_approve_and_requests_flow(self):
        """注册与授权解耦的服务端操作面：joins approve 可零项目（纯注册）；
        worker 提案经 requests pending → approve 落 grants，未暴露项目拒绝。"""
        import hashlib
        from codeloop.cp.registry import Registry
        repo = Path(__file__).resolve().parents[3]
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'cp_server.py'
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / 'server'
            projects_root = Path(temp) / 'projects'
            exposed_dir = projects_root / 'a__b'
            exposed_dir.mkdir(parents=True)
            (exposed_dir / 'control-plane.json').write_text(
                json.dumps({'remote': True}), encoding='utf-8')

            def run(*args):
                return subprocess.run(
                    [sys.executable, str(script), '--home', str(home)] + list(args),
                    capture_output=True, text=True, cwd=str(repo))

            self.assertEqual(run('init').returncode, 0)
            server_json = json.loads((home / 'server.json').read_text(encoding='utf-8'))
            server_json['projects_root'] = str(projects_root)
            (home / 'server.json').write_text(json.dumps(server_json), encoding='utf-8')

            registry = Registry(str(home / 'registry.sqlite'))
            try:
                registry.create_join('gpu-box',
                                     hashlib.sha256(b'pairing-secret').hexdigest(),
                                     cert_fp='', agent_version='1')
            finally:
                registry.close()

            # 纯注册：零项目批准合法
            approved = run('joins', 'approve', '--name', 'gpu-box')
            self.assertEqual(0, approved.returncode, approved.stderr)
            result = json.loads(approved.stdout)['result']
            self.assertEqual([], result['projects'])

            # worker 凭配对密钥取 token（consume 时才建机器行），机器存在且零 grants
            registry = Registry(str(home / 'registry.sqlite'))
            try:
                out = registry.consume_join('gpu-box', 'pairing-secret')
                self.assertEqual([], out['projects'])
                machine_id = registry.authenticate(out['token'])['id']
                # worker 提案（HTTP 路由已另行覆盖，这里直接入队）
                req = registry.create_grant_request(machine_id, 'a__b')
                req_ghost = registry.create_grant_request(machine_id, 'ghost__x')
            finally:
                registry.close()

            pending = run('requests', 'pending')
            self.assertEqual(0, pending.returncode, pending.stderr)
            rows = json.loads(pending.stdout)['result']['requests']
            self.assertEqual({'a__b', 'ghost__x'},
                             {r['org_repo'] for r in rows})
            self.assertEqual({'gpu-box'}, {r['machine_name'] for r in rows})

            granted = run('requests', 'approve', '--id', req['id'])
            self.assertEqual(0, granted.returncode, granted.stderr)
            self.assertEqual('a__b', json.loads(granted.stdout)['result']['granted'])

            blocked = run('requests', 'approve', '--id', req_ghost['id'])
            self.assertNotEqual(0, blocked.returncode)
            self.assertIn('PROJECT_NOT_EXPOSED', blocked.stderr)

            rejected = run('requests', 'reject', '--id', req_ghost['id'])
            self.assertEqual(0, rejected.returncode, rejected.stderr)

            registry = Registry(str(home / 'registry.sqlite'))
            try:
                self.assertTrue(registry.has_grant(machine_id, 'a__b'))
                self.assertFalse(registry.has_grant(machine_id, 'ghost__x'))
            finally:
                registry.close()


class ObserverDriverTests(unittest.TestCase):
    """The server's observer driver is the only refresher of the shared store;
    a leftover function-local `from cp import observer` (pre-restructure path)
    killed that thread at every server start, silently downgrading claim/sync
    to inline crawls. This runs the real driver on a temp home: the thread
    must survive at least one tick and exit once the stop event is set."""

    def test_driver_survives_a_tick_and_stops_cleanly(self):
        import cp_server
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg_dir = root / 'projects' / 'a__b'
            cfg_dir.mkdir(parents=True)
            # An unreachable platform: the tick must report it, never raise.
            # timeout 1 keeps the failure fast even where connects are dropped
            # rather than refused.
            (cfg_dir / 'control-plane.json').write_text(json.dumps({
                'platform': {'url': 'http://127.0.0.1:1', 'repo': 'a/b', 'timeout': 1},
                'remote': True,
            }), encoding='utf-8')
            previous = os.environ.get('CODE_LOOP_HOME')
            os.environ['CODE_LOOP_HOME'] = temp
            errors = []
            stop = threading.Event()
            settings = {'observer_period': 0.05, 'projects_root': str(root / 'projects')}
            store = Path(temp) / 'observer.sqlite'

            def run():
                try:
                    cp_server._observer_driver(settings, stop=stop)
                except BaseException as exc:   # the pre-fix ImportError landed here
                    errors.append(exc)

            try:
                thread = threading.Thread(target=run, daemon=True)
                thread.start()
                # The first tick creates the store before it crawls; waiting for
                # it beats a fixed join, and a dead thread ends the wait at once.
                deadline = time.time() + 10
                while not store.exists() and thread.is_alive() and time.time() < deadline:
                    time.sleep(0.05)
                stop.set()
                thread.join(timeout=10)
                self.assertEqual([], errors)
                self.assertFalse(thread.is_alive(), 'driver must exit once stop is set')
                self.assertTrue(store.exists(), 'the first tick must reach the store')
            finally:
                if previous is None:
                    os.environ.pop('CODE_LOOP_HOME', None)
                else:
                    os.environ['CODE_LOOP_HOME'] = previous


if __name__ == '__main__':
    unittest.main()
