#!/usr/bin/env python3
"""The agent's WebSocket client half — the side that was untested when the
reconnect bug shipped. Real sockets against a scripted listener: upgrade,
first-frame-after-101, server close, reconnect with backoff."""
import base64
import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from codeloop import agent as agent_mod
from codeloop.cp import ws as ws_proto


class ScriptedServer:
    """A loopback listener that answers the upgrade and then runs a scripted
    behaviour per connection. Records every handshake so a test can count
    reconnects."""

    def __init__(self, behaviour):
        self.behaviour = behaviour          # fn(sock, index, accept) -> None
        self.handshakes = []
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            index = len(self.handshakes)
            threading.Thread(target=self._serve, args=(conn, index), daemon=True).start()

    def _serve(self, conn, index):
        """Parse the upgrade request and hand the whole wire to the scripted
        behaviour: the script owns every write, so a test can put the 101 and
        the first frame into one segment when that is what it means to prove."""
        try:
            buf = b''
            while b'\r\n\r\n' not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            head = buf.partition(b'\r\n\r\n')[0]
            key = ''
            for line in head.split(b'\r\n')[1:]:
                if line.lower().startswith(b'sec-websocket-key:'):
                    key = line.split(b':', 1)[1].strip().decode()
            self.handshakes.append(key)
            self.behaviour(conn, index, ws_proto.accept_key(key))
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


def handshake_bytes(accept):
    return ('HTTP/1.1 101 Switching Protocols\r\n'
            'Upgrade: websocket\r\nConnection: Upgrade\r\n'
            'Sec-WebSocket-Accept: %s\r\n\r\n' % accept).encode('ascii')


class FakeClient:
    """A transport double that hands the agent a real socket to this server."""

    def __init__(self, port):
        self.host = '127.0.0.1'
        self.port = port
        self.token = 'tok'

    def raw_socket(self, timeout=10.0):
        return socket.create_connection((self.host, self.port), timeout=timeout)

    def call(self, method, path, body=None, token=None, raw_body=None):
        return {'ok': True}


def make_agent(port, **kw):
    root = Path(kw.pop('root', Path(__file__).resolve().parents[1] / 'tmp' / 'agent-test'))
    return agent_mod.WorkerAgent(
        server_url='http://127.0.0.1:%d' % port, token='tok',
        client=FakeClient(port), popen_fn=lambda *a, **k: type(
            'P', (), {'pid': 1})(), **kw)


class FirstFrameTests(unittest.TestCase):
    """A frame the server flushes immediately after the 101 arrives in the
    same TCP read as the handshake — it must not be dropped (the catch-up
    dispatch path does exactly that)."""

    def test_frame_sent_with_the_101_is_executed(self):
        frame = ws_proto.encode_text(json.dumps(
            {'type': 'command', 'command': {'id': 1, 'action': 'review_round',
                                            'number': 88, 'project': 'a__b'}}))

        def behaviour(conn, index, accept):
            # ONE sendall: the 101 and the first frame travel together, which
            # is exactly what a server that dispatches before entering its
            # read loop produces
            conn.sendall(handshake_bytes(accept) + frame)
            time.sleep(0.4)

        server = ScriptedServer(behaviour)
        executed = []
        agent = make_agent(server.port)
        agent.execute = lambda command: executed.append(command)
        try:
            stop = threading.Event()
            thread = threading.Thread(target=agent.run, args=(stop,), daemon=True)
            thread.start()
            deadline = time.time() + 5
            while time.time() < deadline and not executed:
                time.sleep(0.05)
            stop.set()
            self.assertEqual([{'id': 1, 'action': 'review_round', 'number': 88,
                               'project': 'a__b'}], executed)
        finally:
            server.stop()

    def test_leftover_bytes_are_kept_by_read_head(self):
        """Direct unit pin: _read_head returns the tail, never discards it."""
        left, right = socket.socketpair()
        try:
            right.sendall(b'HTTP/1.1 101 X\r\nA: b\r\n\r\nEXTRA')
            head, rest = make_agent(1)._read_head(left)
            self.assertTrue(head.endswith(b'A: b'))
            self.assertEqual(b'EXTRA', rest)
        finally:
            left.close()
            right.close()


class ReconnectTests(unittest.TestCase):
    """The read thread dying must end the connection lifetime so run()
    reconnects — a silent reader used to leave the agent 'connected' forever."""

    def test_server_close_triggers_reconnect(self):
        def behaviour(conn, index, accept):
            conn.sendall(handshake_bytes(accept))
            time.sleep(0.1)      # close right after the upgrade

        server = ScriptedServer(behaviour)
        agent = make_agent(server.port)
        stop = threading.Event()
        thread = threading.Thread(target=agent.run, args=(stop,), daemon=True)
        try:
            thread.start()
            deadline = time.time() + 15
            while time.time() < deadline and len(server.handshakes) < 2:
                time.sleep(0.05)
            self.assertGreaterEqual(len(server.handshakes), 2,
                                    'agent did not reconnect after the server closed')
        finally:
            stop.set()
            server.stop()

    def test_protocol_violation_also_reconnects(self):
        """A close frame (1002) is the server telling us to go away politely —
        the lifetime must end too."""
        def behaviour(conn, index, accept):
            conn.sendall(handshake_bytes(accept) + ws_proto.encode_close(1002))
            time.sleep(0.2)

        server = ScriptedServer(behaviour)
        agent = make_agent(server.port)
        stop = threading.Event()
        thread = threading.Thread(target=agent.run, args=(stop,), daemon=True)
        try:
            thread.start()
            deadline = time.time() + 15
            while time.time() < deadline and len(server.handshakes) < 2:
                time.sleep(0.05)
            self.assertGreaterEqual(len(server.handshakes), 2)
        finally:
            stop.set()
            server.stop()

    def test_accept_key_mismatch_is_refused(self):
        """A server that does not echo our accept key is not our server."""
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        server.listen(1)

        def serve():
            conn, _ = server.accept()
            conn.recv(4096)
            conn.sendall(b'HTTP/1.1 101 X\r\nSec-WebSocket-Accept: wrong\r\n\r\n')
            time.sleep(0.2)
            conn.close()

        threading.Thread(target=serve, daemon=True).start()
        agent = make_agent(server.getsockname()[1])
        stop = threading.Event()
        try:
            # a watchdog: if the accept check regresses, this must FAIL, not hang
            watchdog = threading.Timer(10, stop.set)
            watchdog.start()
            try:
                with self.assertRaises(ConnectionError) as caught:
                    agent._connect_and_serve(stop)
            finally:
                watchdog.cancel()
            self.assertIn('Sec-WebSocket-Accept', str(caught.exception))
        finally:
            stop.set()
            server.close()


class BackoffTests(unittest.TestCase):
    def test_backoff_is_bounded_and_never_instantly_zero(self):
        for attempt in range(0, 12):
            for _ in range(20):
                value = agent_mod.backoff_seconds(attempt)
                self.assertGreater(value, 0)
                self.assertLessEqual(value, agent_mod.MAX_BACKOFF_SECONDS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
