#!/usr/bin/env python3
"""Remote worker end-to-end over loopback HTTP: real git worktrees, real
verification subprocesses, policy served by the coordinator, local push."""
import copy
import gc
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.cp.client import Client
from codeloop.cp.registry import Registry
from codeloop.cp.runtime import EXAM_PATH, load_global_worker_config
from codeloop.cp.server import Coordinator, make_server
from codeloop.cp.service import Service
from codeloop.cp.worker import WorkerService


class Platform:
    def __init__(self):
        self.data = {'base_sha': 'base1', 'issues': [], 'prs': []}
        self.writes = []
        self.files = {}       # repository paths -> raw content (absent = 404)
        self.raw_calls = []   # (path, ref) of every exam read

    def snapshot(self, known=None):
        return copy.deepcopy(self.data)

    def raw_file(self, path, ref):
        self.raw_calls.append((path, ref))
        return self.files.get(path)

    def pr_files(self, number):
        return []
    def find(self, marker, action, payload):
        return next((r for a, p, r in self.writes if marker in p.get('body', '')), None)

    def write(self, action, payload):
        receipt = {'number': 100 + len(self.writes)}
        self.writes.append((action, payload, receipt))
        return receipt


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args], stderr=subprocess.DEVNULL,
                                   text=True).strip()


def _serve_forever(httpd):
    """Start ``serve_forever`` on a daemon thread and return the thread.

    issue #199：teardown 必须 shutdown → join → server_close 顺序化——负载下
    shutdown() 后线程仍可能对已关闭的 socket 做 select（WinError 10038）。
    """
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return thread


def _stop_server(httpd, thread=None):
    """Ordered teardown for a server started by ``_serve_forever``."""
    httpd.shutdown()
    if thread is not None:
        thread.join(timeout=10)
    httpd.server_close()


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Keep the shared observer store out of the real data home.
        self._home = os.environ.get('CODE_LOOP_HOME')
        os.environ['CODE_LOOP_HOME'] = self.tmp.name
        self.platform = Platform()
        self.registry = Registry(str(self.root / 'registry.sqlite'))
        self.coordinator = Coordinator(self.root / 'projects', self.root / 'registry.sqlite',
                                       service_factory=self.factory)
        self.httpd = make_server(self.coordinator, '127.0.0.1', 0)
        self.port = self.httpd.server_address[1]
        self._serve_thread = _serve_forever(self.httpd)
        # Server-side project config: policy authority (verification included).
        cfg_dir = self.root / 'projects' / 'a__b'
        cfg_dir.mkdir(parents=True)
        (cfg_dir / 'control-plane.json').write_text(json.dumps({
            'platform': {'url': 'https://gitea.invalid', 'repo': 'a/b'}, 'remote': True,
            'verification': [[sys.executable, '-c', 'pass']],
        }), encoding='utf-8')
        # Worker machine: its own clone, worktree and bare origin for pushes.
        repo = self.root / 'repo'
        repo.mkdir()
        git(repo, 'init', '-b', 'main')
        git(repo, 'config', 'user.email', 't@i')
        git(repo, 'config', 'user.name', 'T')
        (repo / 'README.md').write_text('x\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'initial')
        bare = self.root / 'origin.git'
        git(repo, 'init', '--bare', str(bare))
        git(repo, 'remote', 'add', 'origin', str(bare))
        git(repo, 'push', '-u', 'origin', 'main')
        self.trees = self.root / 'trees'
        self.wt = self.trees / 'issue-1'
        git(repo, 'worktree', 'add', '-b', 'fix/mocx/bug', str(self.wt))
        self.repo = repo
        # Enroll one machine with a grant on a__b.
        code = self.registry.create_enroll_code('dev-b', ['a__b'])
        self.raw_token = Client({'url': self.url(), 'project': '_'}).call(
            'POST', '/api/v1/enroll', {'code': code}, token='')['token']

    def tearDown(self):
        _stop_server(self.httpd, self._serve_thread)
        self.registry.close()
        gc.collect()
        self.tmp.cleanup()
        if self._home is None:
            os.environ.pop('CODE_LOOP_HOME', None)
        else:
            os.environ['CODE_LOOP_HOME'] = self._home

    def factory(self, key, cfg, authorization):
        service = Service(cfg, self.platform, authorization=authorization)
        self.addCleanup(lambda: None)
        return service

    def url(self):
        return 'http://127.0.0.1:%d' % self.httpd.server_address[1]

    def worker(self, name='dev-b', config_dir='a__b'):
        """A fresh WorkerService in its own process-shaped isolation.

        Worker machines hold one global file pointing at the CP server; the
        project key rides in the file here (the test origin is a local path)
        and machine-wide execution overrides name this repo's worktrees."""
        cfg_dir = self.root / 'worker-home' / config_dir
        cfg_dir.mkdir(parents=True, exist_ok=True)
        path = self.root / 'worker-home' / 'control-plane.json'
        path.write_text(json.dumps({'server': {'url': self.url(), 'project': config_dir},
                                    'worktree_root': str(self.trees)}), encoding='utf-8')
        config = load_global_worker_config(path, cwd=str(self.repo))
        client = Client(config['server'], token=self.raw_token)
        return WorkerService(config, client=client)

    def issue(self):
        return {'number': 1, 'state': 'open', 'title': '[Content] bug', 'body': 'evidence',
                'labels': [], 'comments': []}

    def test_discover_lifecycle_over_http(self):
        w = self.worker()
        w.sync()
        self.platform.data['issues'] = [self.issue()]
        w.sync()
        execution = w.claim('discover', 'inspector')
        self.assertIsNotNone(execution)
        self.assertEqual(execution['worker'], 'dev-b/inspector')
        w.report(execution['id'], execution['token'],
                 {'decision': 'CONFIRMED', 'reviewer': 'remote-verifier', 'summary': 'proof',
                  'action': 'create_issue', 'payload': {'title': '[Content] bug',
                                                        'body': 'evidence'}})
        receipt = w.publish(execution['id'], execution['token'])
        self.assertEqual(receipt['number'], 100)
        self.assertEqual(len(self.platform.writes), 1)
        self.assertIn('code-loop **discover**', self.platform.writes[0][1]['body'])
        # D2: the commit derived the terminal state server-side; no finish.
        status = w.status()
        self.assertEqual(status['executions'].get('SUCCEEDED'), 1)

    def test_handler_flow_uses_policy_cache_and_pushes_locally(self):
        self.platform.data['issues'] = [self.issue()]
        w = self.worker()
        w.sync()
        execution = w.claim('handler', 'builder')
        self.assertIsNotNone(execution)
        # A second process-shaped instance must pick the policy up from cache.
        w2 = self.worker()
        w2.bind(execution['id'], execution['token'], str(self.wt), 'fix/mocx/bug')
        result = w2.verify(execution['id'], execution['token'])
        self.assertTrue(result['passed'])
        w2.report(execution['id'], execution['token'],
                  {'decision': 'APPROVE', 'reviewer': 'other-reviewer', 'summary': 'done',
                   'action': 'create_pr', 'payload': {'title': 'fix', 'body': 'Closes #1\n\ndone',
                                                      'head': 'fix/mocx/bug', 'base': 'main'}})
        receipt = w2.publish(execution['id'], execution['token'])
        self.assertEqual(receipt['number'], 100)
        branches = subprocess.check_output(['git', '--git-dir', str(self.root / 'origin.git'),
                                            'branch', '--list', 'fix/mocx/bug'], text=True)
        self.assertIn('fix/mocx/bug', branches)
        # D2: publish derived the terminal state server-side.
        status = w2.status()
        self.assertEqual(status['executions'].get('SUCCEEDED'), 1)

    def test_repo_exam_reaches_the_worker_through_the_claim(self):
        """端到端：服务端按 base_sha 读仓库考题 → policy 下发 → worker verify
        执行仓库命令（机器 config 的 verification 让位）。"""
        self.platform.files[EXAM_PATH] = json.dumps(
            {'verification': [[sys.executable, '-c', 'print("repo exam ran")']]})
        self.platform.data['issues'] = [self.issue()]
        w = self.worker()
        w.sync()
        execution = w.claim('handler', 'builder')
        w.bind(execution['id'], execution['token'], str(self.wt), 'fix/mocx/bug')
        result = w.verify(execution['id'], execution['token'])
        self.assertTrue(result['passed'])
        self.assertEqual([sys.executable, '-c', 'print("repo exam ran")'],
                         result['checks'][0]['argv'])
        self.assertEqual([(EXAM_PATH, 'base1')], self.platform.raw_calls)

    def test_exam_fast_tier_handler_runs_fast_reviewer_stays_full(self):
        """分层考题端到端（2026-09-21）：handler 轮跑 verification_fast 加速层；
        reviewer 轮保持全量 verification（人工合并前的正确性地板）。kind 由
        bind 应答落进 worker bind context，独立 CLI 进程的 verify 仍可解析。"""
        self.platform.files[EXAM_PATH] = json.dumps({
            'verification': [[sys.executable, '-c', 'print("full tier ran")']],
            'verification_fast': [[sys.executable, '-c', 'print("fast tier ran")']]})
        self.platform.data['issues'] = [self.issue()]
        w = self.worker()
        w.sync()
        handler = w.claim('handler', 'builder')
        # handler 用独立分支 worktree：分支租约（branch-fix/mocx/bug 属 reviewer
        # 项资源）不得被 handler 占住，否则下面的 reviewer claim 撞互斥锁。
        wt_h = self.trees / 'issue-2'
        git(self.repo, 'worktree', 'add', '-b', 'fix/mocx/other', str(wt_h))
        w.bind(handler['id'], handler['token'], str(wt_h), 'fix/mocx/other')
        self.assertEqual([sys.executable, '-c', 'print("fast tier ran")'],
                         w.verify(handler['id'], handler['token'])['checks'][0]['argv'])
        # reviewer：同机新 claim，独立 worktree/分支（分支租约互不冲突），
        # 同一份考题必须回落全量层。
        self.platform.data['prs'] = [
            {'number': 2, 'state': 'open', 'title': 'fix', 'body': 'x',
             'head': {'sha': git(self.repo, 'rev-parse', 'HEAD'), 'ref': 'fix/mocx/bug'},
             'base': {'sha': 'base1', 'ref': 'main'}, 'merged': False,
             'mergeable': True, 'draft': False, 'comments': [], 'reviews': [], 'checks': []}]
        # 快照 TTL 内第二遍 sync 吃缓存（新 PR 不可见）——与 ExamTests.expire_observer
        # 同款：把 observer 的 synced_at 归零，强制下一遍 sync 重新爬取。
        odb = sqlite3.connect(str(Path(os.environ['CODE_LOOP_HOME']) / 'observer.sqlite'))
        odb.execute("UPDATE meta SET value=? WHERE key='synced_at'", (json.dumps(0),))
        odb.commit()
        odb.close()
        w.sync()
        review = w.claim('reviewer', 'inspector')
        self.assertIsNotNone(review)
        wt2 = self.trees / 'review-pr-2'
        git(self.repo, 'worktree', 'add', '-b', 'feature/mocx/review-pr-2', str(wt2))
        w.bind(review['id'], review['token'], str(wt2), 'feature/mocx/review-pr-2')
        self.assertEqual([sys.executable, '-c', 'print("full tier ran")'],
                         w.verify(review['id'], review['token'])['checks'][0]['argv'])

    def test_claim_freezes_the_exam_per_execution(self):
        """同机并发 claim 会刷新共享 policy 缓存；在途执行的 verify 必须仍
        用领取时冻结的考题（服务端版本 hash 仍兜底，冻结消灭无谓重跑）。"""
        self.platform.data['issues'] = [self.issue()]
        w = self.worker()
        w.sync()
        execution = w.claim('handler', 'builder')
        frozen_path = Path(w.cfg['state_dir']) / 'executions' / (execution['id'] + '.policy.json')
        frozen = json.loads(frozen_path.read_text(encoding='utf-8'))
        self.assertEqual([[sys.executable, '-c', 'pass']], frozen['verification'])
        # Another claim on this machine swaps the shared cache under the round.
        (Path(w.cfg['state_dir']) / 'policy.json').write_text(
            json.dumps(dict(frozen, verification=[[sys.executable, '-c', 'raise SystemExit(9)']])),
            encoding='utf-8')
        w.bind(execution['id'], execution['token'], str(self.wt), 'fix/mocx/bug')
        self.assertTrue(w.verify(execution['id'], execution['token'])['passed'])

    def test_verify_falls_back_to_the_shared_cache_without_a_freeze(self):
        self.platform.data['issues'] = [self.issue()]
        w = self.worker()
        w.sync()
        execution = w.claim('handler', 'builder')
        (Path(w.cfg['state_dir']) / 'executions' / (execution['id'] + '.policy.json')).unlink()
        w.bind(execution['id'], execution['token'], str(self.wt), 'fix/mocx/bug')
        self.assertTrue(w.verify(execution['id'], execution['token'])['passed'])

    def test_worker_conflict_resolution_pushes_to_pr_branch(self):
        # PR #2 mergeable=false → PR_CONFLICT_RESOLUTION repair item; the
        # worker runs on a temp repair branch but the push must land on the
        # original PR branch (target_branch resolved server-side).
        self.platform.data['prs'] = [{'number': 2, 'state': 'open', 'title': 'fix',
                                      'body': 'Closes #1',
                                      'head': {'sha': git(self.repo, 'rev-parse', 'HEAD'),
                                               'ref': 'fix/mocx/bug'},
                                      'base': {'sha': 'base1', 'ref': 'main'}, 'merged': False,
                                      'mergeable': False, 'draft': False,
                                      'comments': [], 'reviews': [], 'checks': []}]
        w = self.worker()
        w.sync()
        execution = w.claim('handler', 'builder')
        self.assertIsNotNone(execution)
        # Temp repair branch starting at the reviewed PR head.
        wt2 = self.trees / 'repair-pr-2'
        git(self.repo, 'worktree', 'add', '-b', 'feature/mocx/repair-pr-2', str(wt2))
        w.bind(execution['id'], execution['token'], str(wt2), 'feature/mocx/repair-pr-2')
        Path(wt2, 'resolved.txt').write_text('merged\n', encoding='utf-8')
        git(wt2, 'add', '.')
        git(wt2, 'commit', '-m', 'resolve conflict')
        resolved = git(wt2, 'rev-parse', 'HEAD')
        w.verify(execution['id'], execution['token'])
        w.report(execution['id'], execution['token'],
                 {'decision': 'APPROVE', 'reviewer': 'other-reviewer', 'summary': 'resolved',
                  'action': 'edit_pr', 'payload': {'number': 2, 'title': 'fix',
                                                   'body': 'conflict resolved'}})
        w.publish(execution['id'], execution['token'])
        bare = self.root / 'origin.git'
        self.assertEqual(resolved, git(bare, 'rev-parse', 'refs/heads/fix/mocx/bug'))
        with self.assertRaises(subprocess.CalledProcessError):
            git(bare, 'rev-parse', 'refs/heads/feature/mocx/repair-pr-2')
        status = w.status()
        self.assertEqual(status['executions'].get('SUCCEEDED'), 1)

    def test_two_machines_mutual_exclusion(self):
        code = self.registry.create_enroll_code('dev-c', ['a__b'])
        token_c = Client({'url': self.url(), 'project': '_'}).call(
            'POST', '/api/v1/enroll', {'code': code}, token='')['token']
        self.platform.data['issues'] = [self.issue()]
        a, c = self.worker(), None
        c_home = self.root / 'worker-home-c'
        c_home.mkdir(parents=True, exist_ok=True)
        path = c_home / 'control-plane.json'
        path.write_text(json.dumps({'server': {'url': self.url(), 'project': 'a__b'},
                                    'worktree_root': str(self.trees)}), encoding='utf-8')
        c = WorkerService(load_global_worker_config(path, cwd=str(self.repo)),
                          client=Client(load_global_worker_config(path, cwd=str(self.repo))['server'],
                                        token=token_c))
        a.sync()
        first = a.claim('discover', 'w')
        self.assertIsNotNone(first)
        self.assertIsNone(c.claim('discover', 'w'))

    def test_server_down_fails_closed(self):
        w = self.worker()
        self.httpd.shutdown()
        self.httpd.server_close()
        with self.assertRaisesRegex(ValueError, 'CP_SERVER'):
            w.sync()

    def test_pin_compare_is_case_insensitive(self):
        """enroll --pin accepts an uppercase SHA-256 fingerprint: hex is
        case-insensitive, openssl prints it uppercase, so either form must
        pass the out-of-band check (issue #137)."""
        import hashlib
        import ssl
        import http.server
        import socket
        from codeloop.cp.client import Client, ClientError

        # Self-signed cert for a throwaway TLS server.
        cert = self.root / 'cert.pem'
        key = self.root / 'key.pem'
        subprocess.run(
            ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
             '-keyout', str(key), '-out', str(cert),
             '-subj', '/CN=127.0.0.1'],
            check=True, capture_output=True)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')

            def log_message(self, *args):
                pass

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('127.0.0.1', 0))
        sock.listen(5)
        port = sock.getsockname()[1]
        httpd = http.server.HTTPServer(('127.0.0.1', port), Handler, bind_and_activate=False)
        httpd.socket = ctx.wrap_socket(sock, server_side=True)
        _serve_thread = _serve_forever(httpd)
        self.addCleanup(_stop_server, httpd, _serve_thread)

        # Observed fingerprint (lowercase hex) and its uppercase form.
        client = Client({'url': 'https://127.0.0.1:%d' % port, 'project': '_'})
        conn, _ = client._connect(pin_expected=None)
        der = conn.sock.getpeercert(binary_form=True)
        digest = hashlib.sha256(der).hexdigest()
        conn.close()

        # Uppercase pin must pass (was rejected before the fix).
        conn, _ = client._connect(pin_expected=digest.upper())
        conn.close()
        # Lowercase still passes.
        conn, _ = client._connect(pin_expected=digest)
        conn.close()
        # A genuinely different fingerprint still fails.
        with self.assertRaisesRegex(ClientError, 'CP_SERVER_PIN_MISMATCH'):
            client._connect(pin_expected='0' * 64)

    def test_pairing_tofu_allowed_on_non_loopback_first_contact(self):
        """Pairing mode must reach a non-loopback HTTPS server on first
        contact without a pre-seeded pin: the server compares the reported
        fingerprint at approve time (issue #148)."""
        import hashlib
        import ssl
        import http.server
        import socket
        from codeloop.cp.client import Client, ClientError

        cert = self.root / 'cert2.pem'
        key = self.root / 'key2.pem'
        subprocess.run(
            ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
             '-keyout', str(key), '-out', str(cert),
             '-subj', '/CN=cp.lan'],
            check=True, capture_output=True)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')

            def log_message(self, *args):
                pass

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('127.0.0.1', 0))
        sock.listen(5)
        port = sock.getsockname()[1]
        httpd = http.server.HTTPServer(('127.0.0.1', port), Handler, bind_and_activate=False)
        httpd.socket = ctx.wrap_socket(sock, server_side=True)
        _serve_thread = _serve_forever(httpd)
        self.addCleanup(_stop_server, httpd, _serve_thread)

        # 127.0.0.1 is loopback, so patch _loopback to force the non-loopback
        # branch (the pairing fix targets first contact to a remote server).
        from unittest import mock
        client = Client({'url': 'https://127.0.0.1:%d' % port, 'project': '_'})
        # Without allow_tofu the first contact is refused (non-loopback path).
        with mock.patch('codeloop.cp.client._loopback', return_value=False):
            with self.assertRaisesRegex(ClientError, 'CP_SERVER_PIN_REQUIRED'):
                client._connect(pin_expected=None)
            # Pairing mode proceeds and writes the TOFU pin.
            conn, _ = client._connect(pin_expected=None, allow_tofu=True)
            der = conn.sock.getpeercert(binary_form=True)
            digest = hashlib.sha256(der).hexdigest()
            conn.close()
            self.assertEqual(digest, client._connect(pin_expected=None)[1])

    def test_worker_cannot_manage_machines(self):
        w = self.worker()
        with self.assertRaisesRegex(ValueError, 'MACHINE_MANAGEMENT_REMOTE'):
            w.enable()
        # recover was removed from the protocol; the worker never had a local
        # copy and the CLI subcommand is gone.
        self.assertFalse(hasattr(w, 'recover'))
        self.assertFalse(hasattr(w, 'finish'))
        self.assertFalse(hasattr(w, 'heartbeat'))

    def test_claim_without_worker_uses_the_loop_as_suffix(self):
        """--worker is optional: a remote carrier sends only the session label
        and the server prefixes the enrolled machine name (cp/server.py), so an
        omitted --worker yields <machine>/<loop>, never a bare machine name
        that no one can attribute to a loop."""
        w = self.worker()
        w.sync()
        self.platform.data['issues'] = [self.issue()]
        w.sync()
        execution = w.claim('discover', None)
        self.assertIsNotNone(execution)
        self.assertEqual('dev-b/discover', execution['worker'])

    def test_blank_worker_is_treated_as_omitted(self):
        w = self.worker()
        w.sync()
        self.platform.data['issues'] = [self.issue()]
        w.sync()
        execution = w.claim('discover', '   ')
        self.assertEqual('dev-b/discover', execution['worker'])

    def test_operator_act_data_plane_delegates_to_executor(self):
        """review MAJOR-3: a session process has no executor of its own, so
        `POST /api/v1/operator/act` must carry the act into the serve-host
        executor (wired verbs) — and the fencing token must hold."""
        import sys as _sys
        from codeloop.operator import face as op_face
        from codeloop.operator import store as op_store
        ostore = op_store.open_store()
        try:
            item, _ = op_store.put_signal(ostore, source='alert', kind='loop_stall',
                                          key='k1', payload={'message': 'stall'},
                                          org_repo='mocx__skills')
            claim = op_face.claim(ostore, worker='HOST/operator', ttl=1_800,
                                  item_id=item['id'])
            # serve-side executor surface injected the way cp_server wires it.
            # wire() mutates the module-global WIRED/_EXECUTOR, so restore the
            # prior surface afterwards to keep sibling test files hermetic.
            saved_wired, saved_pre_exec = set(op_face.WIRED), op_face._EXECUTOR
            try:
                op_face.wire({'collect_refresh', 'command_resend'},
                             lambda verb, target, detail: {'ok': True, 'verb': verb})
                client = Client({'url': self.url(), 'project': '_'},
                                token=self.raw_token)
                receipt = client.call(
                    'POST', '/api/v1/operator/act',
                    {'verb': 'collect_refresh', 'execution': claim['id'],
                     'token': claim['token'], 'target': '', 'detail': ''})
                self.assertTrue(receipt['receipt']['ok'])
                # wrong token is refused (fencing), not executed
                with self.assertRaisesRegex(ValueError,
                                            'missing, fenced, or terminal'):
                    client.call('POST', '/api/v1/operator/act',
                                {'verb': 'collect_refresh',
                                 'execution': claim['id'],
                                 'token': claim['token'] + 1,
                                 'target': '', 'detail': ''})
            finally:
                op_face.WIRED.clear(); op_face.WIRED.update(saved_wired)
                op_face._EXECUTOR = saved_pre_exec
        finally:
            ostore.db.close()


class VerbSurfaceTests(unittest.TestCase):
    """The unification contract: one CLI verb surface, two carriers."""

    def test_local_and_worker_share_verb_surface(self):
        from codeloop.cp.service import Service
        from codeloop.cp.worker import WorkerService
        verbs = ('sync', 'status', 'items', 'claim', 'bind', 'verify',
                 'report', 'publish', 'enable', 'disable', 'reconcile')
        for verb in verbs:
            self.assertTrue(callable(getattr(Service, verb, None)), 'Service.' + verb)
            self.assertTrue(callable(getattr(WorkerService, verb, None)), 'WorkerService.' + verb)


class CliWorkerTests(unittest.TestCase):
    def test_status_through_global_worker_config(self):
        import os
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'control_plane.py'
        repo = Path(__file__).resolve().parents[3]
        # No server here: expect a fail-closed JSON with nonzero exit. The CLI
        # runs in global mode (--config omitted) with the data home redirected.
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / 'code-loop'
            home.mkdir()
            (home / 'control-plane.json').write_text(
                json.dumps({'server': {'url': 'http://127.0.0.1:1'}}), encoding='utf-8')
            env = dict(os.environ, CODE_LOOP_HOME=str(home))
            proc = subprocess.run([sys.executable, str(script), 'status'],
                                  capture_output=True, text=True, cwd=str(repo), env=env)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('CP_SERVER', proc.stdout)


if __name__ == '__main__':
    unittest.main()
