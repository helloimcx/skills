"""HTTP transport for remote CP workers: TLS pinning, bearer auth, no retries.

Mirrors the Gitea adapter discipline: token from env then machine-local
credentials file, TLS verification must hold (pinned CA or trust-on-first-use
fingerprint), no automatic retry of any request.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import ssl
from urllib.parse import urlsplit

from .protocol import PROTOCOL_HEADER, PROTOCOL_VERSION, MAX_BODY

AGENT_VERSION = '1'


class ClientError(Exception):
    pass


def credentials_dir():
    # Redirectable for tests (CODE_LOOP_HOME), same convention as the
    # observer store: a machine-global credentials dir would otherwise let
    # one test's pin/token leak into the next on a reused ephemeral port.
    from .runtime import data_home
    return data_home() / 'credentials'


def resolve_token(token_env):
    token = os.environ.get(token_env, '')
    if token:
        return token.strip()
    token_file = credentials_dir() / token_env
    if token_file.is_file():
        return token_file.read_text(encoding='utf-8').strip()
    return ''


def pin_path(url):
    parts = urlsplit(url)
    return credentials_dir() / ('cp-%s_%s.pin' % (parts.hostname, parts.port))


def _loopback(host):
    host = (host or '').lower()
    return host in ('localhost', '::1') or host.startswith('127.')


class Client:
    """Synchronous JSON client for one CP server + project."""

    def __init__(self, server_cfg, token=None):
        self.url = server_cfg['url']
        self.project = server_cfg.get('project', '_')
        self.timeout = server_cfg.get('timeout', 30)
        parts = urlsplit(self.url)
        self.host, self.port = parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80)
        self.https = parts.scheme == 'https'
        self.ca_file = server_cfg.get('ca_file')
        self.token = token if token is not None else resolve_token(
            server_cfg.get('token_env', 'CODE_LOOP_CP_TOKEN'))

    def endpoint(self, verb):
        return '/api/projects/%s/%s' % (self.project, verb)

    def _connect(self, pin_expected=None, allow_tofu=False):
        """Open a connection with TLS verification that always holds.

        pin_expected: explicit fingerprint to verify (enroll out-of-band check).
        allow_tofu: pairing mode — the server compares the worker-reported
        fingerprint at approve time, so a first contact to a non-loopback
        server may proceed and write the TOFU pin (issue #148).
        Order: pinned CA file -> fingerprint pin file -> TOFU on loopback of
        first use (enroll writes the pin). Plain HTTP only on loopback.
        """
        if self.https:
            if self.ca_file:
                context = ssl.create_default_context(cafile=self.ca_file)
                conn = http.client.HTTPSConnection(self.host, self.port, context=context,
                                                   timeout=self.timeout)
                conn.connect()
                return conn, None
            pin_file = pin_path(self.url)
            stored = pin_file.read_text(encoding='utf-8').strip() if pin_file.is_file() else None
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            conn = http.client.HTTPSConnection(self.host, self.port, context=context,
                                               timeout=self.timeout)
            conn.connect()
            der = conn.sock.getpeercert(binary_form=True)
            digest = hashlib.sha256(der).hexdigest()
            # Hex fingerprints are case-insensitive (openssl prints uppercase);
            # normalize the out-of-band pin so either form passes.
            if pin_expected is not None and digest != pin_expected.strip().lower():
                conn.close()
                raise ClientError('CP_SERVER_PIN_MISMATCH: certificate %s != expected %s'
                                  % (digest, pin_expected))
            if stored is None:
                if not _loopback(self.host) and pin_expected is None and not allow_tofu:
                    conn.close()
                    raise ClientError('CP_SERVER_PIN_REQUIRED: first contact to a non-loopback '
                                      'server needs --pin (out-of-band fingerprint check)')
                pin_file.parent.mkdir(parents=True, exist_ok=True)
                pin_file.write_text(digest + '\n', encoding='utf-8')
                stored = digest
            elif stored != digest:
                conn.close()
                raise ClientError('CP_SERVER_PIN_MISMATCH: certificate %s != pinned %s; '
                                  'the server certificate changed' % (digest, stored))
            return conn, digest
        if not _loopback(self.host):
            raise ClientError('CP_SERVER_REQUIRES_TLS: plain http is loopback-only')
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout), None

    def raw_socket(self, timeout=10.0):
        """A plain/TLS socket to the CP server with the same verification
        discipline as ``_connect`` (pinned CA / pin file / loopback plain) —
        the WebSocket upgrade rides this instead of an http.client
        connection. Never writes the TOFU pin: first contact belongs to the
        HTTP path."""
        if not self.https:
            if not _loopback(self.host):
                raise ClientError('CP_SERVER_REQUIRES_TLS: plain http is loopback-only')
            return socket.create_connection((self.host, self.port), timeout=timeout)
        if self.ca_file:
            context = ssl.create_default_context(cafile=self.ca_file)
            return context.wrap_socket(socket.create_connection(
                (self.host, self.port), timeout=timeout), server_hostname=self.host)
        pin_file = pin_path(self.url)
        stored = pin_file.read_text(encoding='utf-8').strip() if pin_file.is_file() else None
        if stored is None and not _loopback(self.host):
            raise ClientError('CP_SERVER_PIN_REQUIRED: first contact to a non-loopback '
                              'server needs --pin (out-of-band fingerprint check)')
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        raw = context.wrap_socket(socket.create_connection(
            (self.host, self.port), timeout=timeout))
        digest = hashlib.sha256(raw.getpeercert(binary_form=True)).hexdigest()
        if stored is not None and digest != stored:
            raw.close()
            raise ClientError('CP_SERVER_PIN_MISMATCH: certificate %s != pinned %s; '
                              'the server certificate changed' % (digest, stored))
        return raw

    def call(self, method, path, body=None, token=None, raw_body=None):
        payload = raw_body if raw_body is not None else (
            json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None)
        headers = {'Content-Type': 'application/json', PROTOCOL_HEADER: str(PROTOCOL_VERSION),
                   'X-CP-Agent-Version': AGENT_VERSION}
        bearer = token if token is not None else self.token
        if bearer:
            headers['Authorization'] = 'Bearer ' + bearer
        if payload is not None and len(payload) > MAX_BODY:
            raise ClientError('CP_REQUEST_TOO_LARGE')
        try:
            conn, _ = self._connect()
            try:
                conn.request(method, path, body=payload, headers=headers)
                response = conn.getresponse()
                data = response.read()
                status = response.status
            finally:
                conn.close()
        except (OSError, http.client.HTTPException) as exc:
            raise ValueError('CP_SERVER_UNREACHABLE: %s' % exc) from None
        try:
            envelope = json.loads(data.decode('utf-8'))
        except ValueError:
            raise ValueError('CP_SERVER_INVALID_RESPONSE: status %s' % status) from None
        if not isinstance(envelope, dict) or 'ok' not in envelope:
            raise ValueError('CP_SERVER_INVALID_RESPONSE: status %s' % status)
        if status >= 400:
            raise ValueError('CP_SERVER_REJECTED: %s %s' % (status, envelope.get('code')))
        if not envelope['ok']:
            raise ValueError(envelope.get('message') or envelope.get('code') or 'CP_SERVER_ERROR')
        return envelope['result']

    def enroll(self, code, agent_version=AGENT_VERSION, token=None):
        return self.call('POST', '/api/v1/enroll',
                         {'code': code, 'agent_version': agent_version}, token=token or '')

    def join(self, name, secret_hash, cert_fp, agent_version=AGENT_VERSION):
        return self.call('POST', '/api/v1/join',
                         {'name': name, 'secret_hash': secret_hash, 'cert_fp': cert_fp,
                          'agent_version': agent_version}, token='')

    def join_poll(self, name, secret=None):
        body = {'name': name}
        if secret is not None:
            body['secret'] = secret
        return self.call('POST', '/api/v1/join/poll', body, token='')

    def request_project(self, project):
        """Worker-proposed grant request — a proposal only: an operator still
        approves it server-side (`cp_server.py requests approve`)."""
        return self.call('POST', '/api/v1/requests', {'project': project})


def enroll_pairing(url, name, config_path=None, token_env='CODE_LOOP_CP_TOKEN',
                   interpreter=None, verify_timeout=None, worktree_root=None,
                   poll_seconds=5, poll_timeout=900, agent_version=AGENT_VERSION):
    """Worker-initiated pairing enrollment: join → wait for operator approval →
    poll for the token. The pairing secret is generated here and only ever
    leaves as a hash until pickup; the cert fingerprint the worker observed is
    reported so the operator's approve auto-compares it server-side (MITM
    detection without the operator copying a pin by hand).

    Registration carries no projects: authorization is proposed afterwards via
    `request-project` and approved server-side, so the global config never
    gets an automatic `server.project` pin (multi-project workers route by
    working directory)."""
    import secrets as _secrets
    import socket
    import time as _time

    name = (name or socket.gethostname()).strip()
    if not name:
        raise ValueError('JOIN_NAME_REQUIRED')
    secret = _secrets.token_urlsafe(32)
    client = Client({'url': url, 'token_env': token_env})
    cert_fp = ''
    if client.https:
        # Pairing mode: the server compares this fingerprint at approve time,
        # so first contact to a non-loopback server may proceed (TOFU write).
        conn, _ = client._connect(pin_expected=None, allow_tofu=True)
        der = conn.sock.getpeercert(binary_form=True)
        cert_fp = hashlib.sha256(der).hexdigest()
        conn.close()
    client.join(name, hashlib.sha256(secret.encode('utf-8')).hexdigest(), cert_fp,
                agent_version=agent_version)
    deadline = _time.time() + poll_timeout
    while True:
        try:
            out = client.join_poll(name, secret=secret)
            if out.get('state') == 'CONSUMED':
                break
        except ValueError:
            # waiting (e.g. JOIN_NOT_APPROVED) — keep polling
            pass
        if _time.time() > deadline:
            raise ValueError('JOIN_TIMEOUT: operator did not approve within %ss '
                             '(server: machines pending → machines approve)'
                             % poll_timeout)
        _time.sleep(poll_seconds)
    token = out['token']
    credentials_dir().mkdir(parents=True, exist_ok=True)
    (credentials_dir() / token_env).write_text(token + '\n', encoding='utf-8')
    if config_path is not None:
        path = Path(config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        cfg = {}
        if path.is_file():
            try:
                cfg = json.loads(path.read_text(encoding='utf-8'))
            except ValueError:
                cfg = {}
        cfg['server'] = {'url': url}
        if interpreter:
            cfg['interpreter'] = interpreter
        if verify_timeout:
            cfg['verify_timeout'] = verify_timeout
        if worktree_root:
            cfg['worktree_root'] = worktree_root
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return {'ok': True, 'state': out['state'], 'name': out['name'],
            'token_file': str(credentials_dir() / token_env)}


def doctor(config_path, token_env='CODE_LOOP_CP_TOKEN'):
    """Local self-check for a worker machine: server reachability, TLS pin,
    config fields, interpreter path and token file. Returns a check list; exit
    non-zero is surfaced by the CLI's main() on ValueError."""
    from pathlib import Path as _Path
    checks = []

    def check(name, ok, detail=''):
        checks.append({'name': name, 'ok': bool(ok), 'detail': detail})

    path = _Path(config_path)
    if not path.is_file():
        raise ValueError('DOCTOR_NO_CONFIG: %s missing — run enroll first' % path)
    try:
        cfg = json.loads(path.read_text(encoding='utf-8'))
    except ValueError as exc:
        raise ValueError('DOCTOR_CONFIG_INVALID: %s' % exc) from None
    server = cfg.get('server') or {}
    check('server.url', bool(server.get('url')), server.get('url') or 'missing')
    if server.get('url'):
        try:
            client = Client(server)
            conn, _ = client._connect(pin_expected=None)
            conn.close()
            check('connectivity+tls', True, 'connected')
        except Exception as exc:  # noqa: BLE001 - report the failure, keep going
            check('connectivity+tls', False, str(exc))
    check('token_file', bool(resolve_token(token_env)), 'CODE_LOOP_CP_TOKEN file/env')
    interpreter = cfg.get('interpreter') or ''
    if interpreter:
        check('interpreter', _Path(interpreter).exists(), interpreter)
    else:
        check('interpreter', False, 'unset — {python} verify will fall back to the runner')
    verify_timeout = cfg.get('verify_timeout')
    check('verify_timeout', verify_timeout and int(verify_timeout) >= 300,
          ('%s' % verify_timeout) if verify_timeout else 'unset (default 120 — too low)')
    _doctor_verify_timeout_dual_source(path, cfg, check)
    return {'config': str(path), 'checks': checks,
            'ok': all(c['ok'] for c in checks)}


def _doctor_verify_timeout_dual_source(path, cfg, check):
    """issue #200：verify_timeout 双真相源——全局 projects.<key> override 与
    项目 config 各管一边，同一条 verify 命令因解析出的 personality 不同拿到
    不同预算。fail-loud：不一致时 WARN（不静默），提示收敛。"""
    for key, overrides in (cfg.get('projects') or {}).items():
        if not isinstance(overrides, dict) or 'verify_timeout' not in overrides:
            continue
        project_cfg = path.parent / 'projects' / key / 'control-plane.json'
        project_timeout = None
        if project_cfg.is_file():
            try:
                project_timeout = json.loads(
                    project_cfg.read_text(encoding='utf-8')).get('verify_timeout')
            except ValueError:
                project_timeout = None
        global_timeout = overrides['verify_timeout']
        if project_timeout is not None and project_timeout != global_timeout:
            check('verify_timeout.%s' % key, False,
                  'project=%s vs global=%s — 双真相源，verify 预算随解析 personality 漂移'
                  % (project_timeout, global_timeout))


def enroll_machine(url, code, expected_pin=None, config_path=None,
                   token_env='CODE_LOOP_CP_TOKEN', agent_version=AGENT_VERSION):
    """One-time machine enrollment: verify the TLS fingerprint out-of-band,
    exchange the one-time code, persist the token and the config `server`
    section. The TOFU pin file is written by the transport on first contact.
    Like pairing, no project pin is written: the code's grants live server-side."""
    client = Client({'url': url, 'token_env': token_env})
    # Spend the fingerprint check before spending the one-time code.
    conn, _ = client._connect(pin_expected=expected_pin)
    conn.close()
    out = client.call('POST', '/api/v1/enroll',
                      {'code': code, 'agent_version': agent_version}, token='')
    credentials_dir().mkdir(parents=True, exist_ok=True)
    (credentials_dir() / token_env).write_text(out['token'] + '\n', encoding='utf-8')
    if config_path is not None:
        path = Path(config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        cfg = {}
        if path.is_file():
            try:
                cfg = json.loads(path.read_text(encoding='utf-8'))
            except ValueError:
                cfg = {}
        cfg['server'] = {'url': url}
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return dict(out, token_file=str(credentials_dir() / token_env))
