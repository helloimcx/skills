#!/usr/bin/env python3
"""WebSocket command channel: RFC 6455 framing vectors, the upgrade route
(auth before 101), and the end-to-end push — ledger enqueue → dispatcher →
WS frame → HTTP command_ack."""
import base64
import io
import json
import os
import socket
import sys
import tempfile
import time
import threading
import unittest
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop.cp import ws
from codeloop.cp.server import Coordinator
from codeloop.cp import ws as ws_proto
from codeloop.cp.service import Service


class _BytesSource(io.BytesIO):
    """BytesIO with a recv() so read_frame can consume it like a socket."""

    def recv(self, n):
        return self.read(n)


class CodecTests(unittest.TestCase):
    def test_accept_key_rfc_example(self):
        # RFC 6455 §1.3 — the handshake's only math, pinned to the spec value.
        self.assertEqual('s3pPLMBiTxaQ9kYGzzhZRbK+xOo=',
                         ws.accept_key('dGhlIHNhbXBsZSBub25jZQ=='))

    def test_client_frame_roundtrip_through_the_server_reader(self):
        raw = ws.encode_client_text('{"type":"pong"}')
        opcode, payload = ws.read_frame(_BytesSource(raw))
        self.assertEqual((ws.OP_TEXT, b'{"type":"pong"}'), (opcode, payload))

    def test_server_frame_read_by_a_client_endpoint(self):
        raw = ws.encode_text('hello')
        opcode, payload = ws.read_frame(_BytesSource(raw), masked_required=False)
        self.assertEqual((ws.OP_TEXT, b'hello'), (opcode, payload))

    def _refused(self, raw, code):
        with self.assertRaises(ws.WsProtocolError) as caught:
            ws.read_frame(_BytesSource(raw))
        self.assertEqual(code, caught.exception.code)

    def test_unmasked_client_frame_is_1002(self):
        frame = bytes([0x81, 0x01]) + b'x'   # text, no mask bit, no mask key
        self._refused(frame, 1002)

    def test_fragmented_frame_is_1002(self):
        frame = bytes([0x01, 0x80, 0, 0, 0, 0])   # FIN clear = continuation chain
        self._refused(frame, 1002)

    def test_oversize_frame_is_1009(self):
        import struct
        frame = bytes([0x81, 0x80 | 127]) + struct.pack('>Q', 65540) + b'\x00' * 4
        self._refused(frame, 1009)

    def test_binary_frame_is_1003(self):
        frame = bytes([0x82, 0x80, 0, 0, 0, 0])
        self._refused(frame, 1003)

    def test_rsv_bits_are_1002(self):
        frame = bytes([0x81 | 0x40, 0x80, 0, 0, 0, 0])
        self._refused(frame, 1002)

    def test_oversize_control_frame_is_1002(self):
        # a ping with the 16-bit length form: control frames cap at 125 bytes
        frame = bytes([0x89, 0x80 | 126, 0, 200]) + bytes(4)
        self._refused(frame, 1002)


class WsChannelTests(unittest.TestCase):
    """End-to-end over loopback: real server, real registry, real ledger."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._home = os.environ.get('CODE_LOOP_HOME')
        os.environ['CODE_LOOP_HOME'] = self.tmp.name
        cfg_dir = self.root / 'projects' / 'a__b'
        cfg_dir.mkdir(parents=True)
        (cfg_dir / 'control-plane.json').write_text(json.dumps(
            {'platform': {'url': 'http://g', 'repo': 'a/b'}, 'remote': True,
             'state_dir': str(cfg_dir / 'state'), 'project_key': 'a__b',
             'snapshot_ttl': 3600}), encoding='utf-8')
        self.coordinator = Coordinator(self.root / 'projects',
                                       self.root / 'registry.sqlite')
        from codeloop.cp.server import make_server
        self.httpd = make_server(self.coordinator, '127.0.0.1', 0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.registry = self.coordinator.registry()
        code = self.registry.create_enroll_code('agent-box', ['a__b'])
        out = self.registry.enroll(code, '1', '127.0.0.1')
        self.token = out['token']
        self.service = self.coordinator.service_for('a__b')

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.registry.close()
        self.service.store.db.close()
        import gc
        gc.collect()
        self.tmp.cleanup()
        if self._home is None:
            os.environ.pop('CODE_LOOP_HOME', None)
        else:
            os.environ['CODE_LOOP_HOME'] = self._home

    def _upgrade(self, token=None, upgrade=True):
        """Raw-socket the WS handshake; returns (socket, status, accept)."""
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=5)
        key = base64.b64encode(b'0123456789abcdef').decode()
        request = ['GET /api/v1/ws HTTP/1.1',
                   'Host: 127.0.0.1:%d' % self.port,
                   'X-CP-Protocol: 1']
        if upgrade:
            request += ['Upgrade: websocket', 'Connection: Upgrade',
                        'Sec-WebSocket-Key: ' + key]
        if token:
            request.append('Authorization: Bearer ' + token)
        sock.sendall(('\r\n'.join(request) + '\r\n\r\n').encode('ascii'))
        buf = b''
        while b'\r\n\r\n' not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        head, _, rest = buf.partition(b'\r\n\r\n')
        status = int(head.split(b'\r\n')[0].split()[1])
        accept = None
        for line in head.split(b'\r\n')[1:]:
            if line.lower().startswith(b'sec-websocket-accept:'):
                accept = line.split(b':', 1)[1].strip().decode()
        return sock, status, accept, rest

    def test_hello_and_agent_options_build_the_projection(self):
        """The frame path end to end (the QA that caught the lost
        WS_PING_SECONDS): a real socket hello carrying agents registers the
        machine AND stores the sanitized projection; a later agent_options
        frame wins; the dispatcher reads both back."""
        sock, status, accept, rest = self._upgrade(token=self.token)
        self.assertEqual(101, status)
        hello = {'type': 'hello', 'machine': 'agent-box',
                 'registrations': ['a__b'],
                 'agents': [{'id': 'pi', 'models': ['selfhost/ds']}]}
        sock.sendall(ws_proto.encode_client_text(json.dumps(hello)))
        time.sleep(0.5)
        d = self.coordinator.dispatcher
        self.assertEqual({'a__b'}, set(d._by_project))
        report = {'type': 'agent_options',
                  'agents': [{'id': 'pi', 'models': ['selfhost/ds']},
                             {'id': 'codex', 'models': ['x/y']}]}
        sock.sendall(ws_proto.encode_client_text(json.dumps(report)))
        time.sleep(0.5)
        cap = d.capability_for('a__b')
        self.assertEqual({'pi': ['selfhost/ds']}, cap['models'])   # whitelist
        self.assertEqual('agent-box', cap['machine'])
        sock.close()

    def test_upgrade_requires_the_bearer_token(self):
        sock, status, accept, _ = self._upgrade(token=None)
        self.assertEqual(401, status)
        self.assertIsNone(accept)
        sock.close()

    def test_wrong_websocket_version_is_refused(self):
        crlf = chr(13) + chr(10)
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=5)
        key = base64.b64encode(b'0123456789abcdef').decode()
        request = (
            'GET /api/v1/ws HTTP/1.1' + crlf
            + 'Host: h' + crlf
            + 'X-CP-Protocol: 1' + crlf
            + 'Authorization: Bearer ' + self.token + crlf
            + 'Upgrade: websocket' + crlf
            + 'Connection: Upgrade' + crlf
            + 'Sec-WebSocket-Key: ' + key + crlf
            + 'Sec-WebSocket-Version: 8' + crlf + crlf)
        sock.sendall(request.encode('ascii'))
        buf = b''
        head_end = (crlf + crlf).encode()
        while head_end not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        self.assertIn(b'400', buf.split(crlf.encode())[0])
        sock.close()


    def test_plain_get_is_not_an_upgrade(self):
        sock, status, accept, _ = self._upgrade(token=self.token, upgrade=False)
        self.assertEqual(400, status)
        sock.close()

    def test_enqueue_pushes_the_command_frame_and_ack_lands(self):
        self.service.enqueue_command('review_round', 88)
        sock, status, accept, rest = self._upgrade(token=self.token)
        self.assertEqual(101, status)
        self.assertEqual(ws.accept_key(base64.b64encode(b'0123456789abcdef').decode()),
                         accept)
        try:
            opcode, payload = ws.read_frame(sock, masked_required=False)
            self.assertEqual(ws.OP_TEXT, opcode)
            message = json.loads(payload.decode('utf-8'))
            self.assertEqual('command', message['type'])
            command = message['command']
            self.assertEqual(('review_round', 88), (command['action'], command['number']))
            row = self.service.latest_command('review_round', 88)
            self.assertEqual(('CLAIMED', 'agent-box'), (row['state'], row['claimed_by']))
            # the agent reports back over the ordinary authenticated HTTP path
            # — a fresh connection, never the WebSocket itself (its byte stream
            # belongs to the frame protocol)
            ack = socket.create_connection(('127.0.0.1', self.port), timeout=5)
            body = json.dumps({'command': command['id'], 'outcome': 'SPAWNED'}).encode()
            request = (b'POST /api/projects/a__b/command_ack HTTP/1.1\r\n'
                       b'Host: h\r\nX-CP-Protocol: 1\r\n'
                       b'Authorization: Bearer ' + self.token.encode() + b'\r\n'
                       b'Content-Type: application/json\r\n'
                       b'Content-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
            ack.sendall(request)
            ack.recv(4096)
            ack.close()
            deadline = __import__('time').time() + 5
            row = None
            while __import__('time').time() < deadline:
                row = self.service.latest_command('review_round', 88)
                if row['state'] == 'SPAWNED':
                    break
                __import__('time').sleep(0.05)
            self.assertEqual('SPAWNED', row['state'])
        finally:
            sock.close()

    def test_upgrade_after_a_stale_socket_replaces_the_registration(self):
        first, status, _, _ = self._upgrade(token=self.token)
        self.assertEqual(101, status)
        second, status2, _, _ = self._upgrade(token=self.token)
        self.assertEqual(101, status2)
        # exactly one live connection remains for the machine — the stale
        # socket's own cleanup must not unregister its replacement. The
        # server registers after flushing the 101, so poll briefly. Identity
        # is by TCP peer port: the registered socket's peer is the agent.
        deadline = __import__('time').time() + 5
        live = []
        while __import__('time').time() < deadline:
            with self.coordinator.dispatcher._lock:
                live = list(self.coordinator.dispatcher._connections.values())
            if len(live) == 1 and live[0].socket.getpeername()[1] == second.getsockname()[1]:
                break
            __import__('time').sleep(0.05)
        self.assertEqual(1, len(live))
        self.assertEqual(second.getsockname()[1], live[0].socket.getpeername()[1])
        first.close()
        second.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
