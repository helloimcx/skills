"""CP server: the network face of the single-machine coordinator.

Every request constructs its own Service/Store/Registry connection — the same
short-lived-connection discipline as one CLI process per invocation. Auth
order: bearer token → machine liveness → (writes only) enabled + grant.
"""
from __future__ import annotations

import json
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time

from .. import agent_runners
from . import ws as ws_proto
from .capabilities import CapabilityProjection, spawn_options
from .protocol import (PROTOCOL_HEADER, PROTOCOL_VERSION, MAX_BODY,
                       ProtocolError, dumps, envelope_fail, envelope_ok)
from .registry import Registry, RegistryError
from .runtime import load_server_config
from .service import Service

READ_VERBS = {'sync', 'status', 'list'}
WRITES = {'claim', 'bind', 'verify', 'report', 'publish/begin', 'publish/commit',
          'command_ack', 'command_progress'}
LOOPS = ('discover', 'handler', 'reviewer')

# How often the per-connection read loop may sit idle before the server pings
# the agent (server.json ws_ping_seconds); two silent periods close the socket.
WS_PING_SECONDS = 30




class Coordinator:
    """Maps project keys to Service instances and owns the registry location."""

    AUTHORIZATION = {'host': 'cp-server', 'managed_by': 'registry'}

    def __init__(self, projects_root, registry_path, service_factory=None):
        self.projects_root = Path(projects_root).expanduser().resolve()
        self.registry_path = Path(registry_path)
        self._service_factory = service_factory
        self.dispatcher = CommandDispatcher(self)
        # Optional operator event sink (2026-09-30): attached to every Store
        # this coordinator hands out, so serve-side CP writes reach the
        # operator subscription at the write point itself.
        self.event_sink = None

    def service_for(self, key):
        if not re.fullmatch(r'[A-Za-z0-9_.\-]+__[A-Za-z0-9_.\-]+', key or ''):
            raise ProtocolError(404, 'PROJECT_NOT_FOUND', 'unknown project key')
        path = self.projects_root / key / 'control-plane.json'
        if not path.is_file():
            raise ProtocolError(404, 'PROJECT_NOT_FOUND', 'unknown project: ' + key)
        try:
            cfg = load_server_config(path)
        except ValueError as exc:
            raise ProtocolError(500, 'CONFIG_INVALID', str(exc)) from None
        if not cfg.get('remote'):
            raise ProtocolError(404, 'PROJECT_NOT_REMOTE',
                                'project is not exposed to remote workers')
        if self._service_factory is not None:
            service = self._service_factory(key, cfg, self.AUTHORIZATION)
        else:
            service = Service(cfg, authorization=self.AUTHORIZATION)
        if self.event_sink is not None:
            service.store.on_event = self.event_sink
        return service

    def exposed_keys(self):
        """Project keys this server serves to workers (remote:true configs)."""
        return [key for key, cfg in self._all_configs() if cfg.get('remote') is True]

    def all_keys(self):
        """Every project this server has a config for — the repair sweep's
        scope (a ledger row must age out even when no worker may serve it)."""
        return [key for key, _ in self._all_configs()]

    def _all_configs(self):
        found = []
        if self.projects_root.is_dir():
            for path in sorted(self.projects_root.glob('*/control-plane.json')):
                try:
                    cfg = json.loads(path.read_text(encoding='utf-8'))
                except ValueError:
                    continue
                found.append((path.parent.name, cfg))
        return found

    def registry(self):
        return Registry(self.registry_path)


class CommandDispatcher(CapabilityProjection):
    """Bridges the per-project command ledger to live WS agents.

    Eligibility is decided here — a machine must be connected, enabled and
    granted for the project (the grant checked fresh in the registry at
    dispatch time); atomicity lives in the ledger (single-winner
    ``mark_dispatched``); delivery is the socket, and a failed send reverts
    the command to PENDING. Triggers are events only: an enqueue (the merged
    UI's click — same process) and an agent's connect (catch-up). There is no
    dispatch scan: a slow or absent consumer leaves the command on the ledger
    where the TTL sweep makes the wait visible (EXPIRED) instead of lossy.
    """

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self._lock = threading.Lock()
        self._connections = {}    # machine_id -> WsConnection
        self._machines = {}       # machine_id -> machine row (at register time)
        self._last_dispatch = {}  # machine_id -> time
        self._registrations = {}      # machine_id -> set(project keys)
        self._by_project = {}         # project key -> set(machine_id)
        self._capabilities = {}   # machine_id -> {models, ts} (live only)

    def register(self, machine, conn):
        with self._lock:
            stale = self._connections.get(machine['id'])
            self._connections[machine['id']] = conn
            self._machines[machine['id']] = machine
        if stale is not None and stale is not conn:
            try:
                stale.close()
            except Exception:
                pass   # the stale read loop notices on its own socket error

    def unregister(self, machine_id, conn=None):
        """Drop the machine's registration — but never a NEWER connection's:
        a stale socket's own cleanup must not unregister its replacement."""
        with self._lock:
            if conn is None or self._connections.get(machine_id) is conn:
                self._connections.pop(machine_id, None)
                for key in self._registrations.pop(machine_id, set()):
                    peers = self._by_project.get(key)
                    if peers is not None:
                        peers.discard(machine_id)
                        if not peers:
                            self._by_project.pop(key, None)
                self._capabilities.pop(machine_id, None)

    def hello(self, machine, registrations, agents=None):
        """Attach the machine's announced registrations to its live
        connection, filtered to what the registry actually grants the machine
        (a self-reported registration for an ungranted project would make the
        preflight lie — dispatch would skip the machine anyway). Replaces the
        previous announcement (a re-hello is the machine's current truth, not
        an addendum). A registry read failure trusts the announcement: grants
        are re-checked at dispatch, so the worst case is one wrong hint."""
        machine_id = machine['id']
        keys = {k for k in (registrations or [])
                if isinstance(k, str) and '__' in k}
        try:
            registry = self.coordinator.registry()
            try:
                keys &= registry.machine_grants(machine_id)
            finally:
                registry.close()
        except Exception:
            pass
        with self._lock:
            for key in self._registrations.get(machine_id, set()) - keys:
                peers = self._by_project.get(key)
                if peers is not None:
                    peers.discard(machine_id)
                    if not peers:
                        self._by_project.pop(key, None)
            for key in keys - self._registrations.get(machine_id, set()):
                self._by_project.setdefault(key, set()).add(machine_id)
            self._registrations[machine_id] = keys
        if agents is not None:
            self._store_capabilities(machine_id, agents)

    def registrations(self, keys):
        """Preflight view per project key, both sides scoped to that project:
        ``online`` = connected machines granted for it (the dispatch-eligible
        candidates), ``registered`` = the subset that announced a registration
        in hello. A machine granted but never helloed (legacy agent pre-upgrade)
        shows online but unregistered — the UI disables the button for such a
        project, so upgrade the agent (or accept the queue-by-CLI path) per
        the rollout order in the change record."""
        try:
            registry = self.coordinator.registry()
            try:
                grants = {}
                for row in registry.db.execute(
                        'SELECT machine_id, org_repo FROM grants'):
                    grants.setdefault(row['org_repo'], set()).add(row['machine_id'])
            finally:
                registry.close()
        except Exception:
            grants = None
        with self._lock:
            def online_for(key):
                candidates = self._connections.keys() if grants is None \
                    else self._connections.keys() & grants.get(key, set())
                return sorted(self._machines[mid]['name'] for mid in candidates)
            return {key: {
                'online': online_for(key),
                'registered': sorted(self._machines[mid]['name']
                                     for mid in self._by_project.get(key, ())
                                     if mid in self._connections),
            } for key in keys}

    def _choose(self, registry, key, machine_id=None, excluded=()):
        """Least-recently-dispatched connected machine granted for `key`.
        Enablement and revocation are re-read from the registry here (the
        registered snapshot is only an identity cache): a machine disabled or
        revoked after connecting must stop receiving commands immediately."""
        try:
            fresh = {row['id']: row for row in registry.list_machines()}
        except Exception:
            fresh = {}
        with self._lock:
            candidates = sorted(
                (self._last_dispatch.get(mid, 0.0), mid, self._machines.get(mid))
                for mid in self._connections)
        for _, mid, cached in candidates:
            if mid in excluded:
                continue
            if machine_id is not None and mid != machine_id:
                continue
            machine = fresh.get(mid, cached)
            if machine is None or machine['revoked'] or not machine['enabled']:
                continue
            try:
                if not registry.has_grant(mid, key):
                    continue
            except RegistryError:
                continue
            return mid
        return None

    def dispatch(self, machine_id=None, keys=None, now=None):
        """Hand every pending command (optionally only to `machine_id`, only
        within `keys`) to a live granted agent. Returns the dispatched count.
        Cheap and idempotent: an event may fire it freely. A machine whose
        socket fails is dropped and the next candidate is tried, so one dead
        connection never parks a command on the ledger."""
        dispatched = 0
        for key in (keys if keys is not None else self.coordinator.exposed_keys()):
            try:
                service = self.coordinator.service_for(key)
            except ProtocolError:
                continue   # not exposed (anymore) / gone — nothing to dispatch
            registry = self.coordinator.registry()
            try:
                while True:
                    pending = service.pending_commands(now=now)
                    if not pending:
                        break
                    command = pending[0]
                    failed_here = set()
                    sent = False
                    while True:
                        mid = self._choose(registry, key, machine_id=machine_id,
                                           excluded=failed_here)
                        if mid is None:
                            break   # nobody left to try for this command
                        with self._lock:
                            conn = self._connections.get(mid)
                            name = self._machines.get(mid, {}).get('name')
                        if conn is None:
                            failed_here.add(mid)
                            continue
                        claimed = service.store.mark_dispatched(command['id'], name,
                                                                now=now)
                        if claimed is None:
                            break   # another event won it; look at the next one
                        command_frame = self._command_frame(claimed, claimed['action'], key)
                        try:
                            conn.send_json({'type': 'command',
                                            'command': command_frame})
                        except Exception as exc:
                            # This socket is unusable: drop exactly it (a newer
                            # connection for the same machine must survive — the
                            # connection object is the identity) and try the
                            # next candidate machine for this command.
                            service.store.revert_pending(command['id'],
                                                         reason=type(exc).__name__,
                                                         now=now)
                            self.unregister(mid, conn)
                            try:
                                conn.close()
                            except Exception:
                                pass
                            failed_here.add(mid)
                            continue
                        self._last_dispatch[mid] = time.time()
                        dispatched += 1
                        sent = True
                        break
                    if not sent:
                        break   # this command waits for the next event
            finally:
                registry.close()
                service.store.db.close()
        return dispatched

    def _command_frame(self, claimed, action, project):
        """On-the-wire frame; spawn options (dialog/cfg picks) ride along only
        when set, else the frame stays the historical shape."""
        extras = {k: claimed[k] for k in ('agent', 'model') if claimed.get(k)}
        frame = {'id': claimed['id'], 'action': action,
                 'number': claimed['number'], 'project': project}
        frame.update(extras)
        return frame

    def dispatch_operator(self, now=None, store=None):
        """Hand pending operator_round commands to the serve host's own agent.
        Operator rounds run here by definition — the machine-level store, the
        dash data and the loopback face are all local — so the candidate set
        is exactly the connection whose registry name is this hostname. One
        live operator round at a time: the ledger keeps the rest PENDING where
        the TTL sweep keeps the wait visible."""
        from ..operator import store as op_store
        machine = socket.gethostname()
        owned = store is not None
        store = store or op_store.open_store()
        dispatched = 0
        try:
            while True:
                with self._lock:
                    conn = next((c for mid, c in self._connections.items()
                                 if self._machines.get(mid, {}).get('name') == machine),
                                None)
                if conn is None:
                    break
                pending = store.pending_commands(now=now)
                if not pending:
                    break
                command = pending[0]
                claimed = store.mark_dispatched(command['id'], machine, now=now)
                if claimed is None:
                    break
                item = op_store.item_for_number(store, command['number'])
                frame = self._command_frame(claimed, 'operator_round', 'operator')
                if item is not None:
                    frame['item'] = item['id']
                try:
                    conn.send_json({'type': 'command', 'command': frame})
                except Exception:
                    store.revert_pending(command['id'], reason='SOCKET', now=now)
                    break
                dispatched += 1
        finally:
            if not owned:
                store.db.close()
        return dispatched


class WsConnection:
    """One live agent socket. Sends are serialized (dispatch can fire from the
    UI request thread while the read loop runs); the read loop answers pings
    and drops the connection on any protocol violation or silent period —
    the agent's reconnect backoff and the ledger's repair sweep absorb both."""

    def __init__(self, sock, machine, dispatcher):
        self.socket = sock
        self.machine = machine
        self.dispatcher = dispatcher
        self._lock = threading.Lock()
        self.last_recv = time.time()

    def send_json(self, payload):
        with self._lock:
            self.socket.sendall(ws_proto.encode_text(
                json.dumps(payload, ensure_ascii=False)))

    def close(self):
        try:
            with self._lock:
                self.socket.sendall(ws_proto.encode_close())
        except Exception:
            pass
        try:
            self.socket.close()
        except Exception:
            pass

    def serve(self, ping_seconds=None):
        if ping_seconds is None:
            ping_seconds = WS_PING_SECONDS   # read at call time: cmd_serve may tune it
        self.socket.settimeout(ping_seconds)
        while True:
            try:
                opcode, payload = ws_proto.read_frame(self.socket)
            except socket.timeout:
                if time.time() - self.last_recv > 2 * ping_seconds:
                    self.close()
                    break
                try:
                    with self._lock:
                        self.socket.sendall(ws_proto.encode_ping())
                except OSError:
                    break
                continue
            except ws_proto.WsProtocolError as exc:
                try:
                    with self._lock:
                        self.socket.sendall(ws_proto.encode_close(exc.code))
                except OSError:
                    pass
                self.close()
                break
            except (ConnectionError, OSError):
                self.close()
                break
            self.last_recv = time.time()
            if opcode == ws_proto.OP_PING:
                try:
                    with self._lock:
                        self.socket.sendall(ws_proto.encode_pong(payload))
                except OSError:
                    break
            elif opcode == ws_proto.OP_PONG:
                continue
            elif opcode == ws_proto.OP_CLOSE:
                try:
                    with self._lock:
                        self.socket.sendall(ws_proto.encode_close())
                except OSError:
                    pass
                break
            elif opcode == ws_proto.OP_TEXT:
                self._handle_text(payload)
            # other text from an agent is unexpected (state writes go over
            # HTTP); ignore it rather than kill the connection over a stray frame.

    def _handle_text(self, payload):
        """One agent TEXT frame. Today the only frame is the connect-time
        hello announcing this machine's registrations; anything unparseable
        or of an unknown type is ignored — a legacy/foreign frame must never
        take the channel down."""
        try:
            message = json.loads(payload.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(message, dict):
            return
        kind = message.get('type')
        if kind == 'hello':
            self.dispatcher.hello(self.machine, message.get('registrations'),
                                  message.get('agents'))
        elif kind == 'agent_options':
            self.dispatcher.capabilities(self.machine, message.get('agents'))


class Handler(BaseHTTPRequestHandler):
    coordinator = None
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):  # quiesce per-request stderr noise
        pass

    def _send(self, status, envelope):
        body = dumps(envelope)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = self.headers.get('Content-Length')
        if length is None:
            raise ProtocolError(400, 'INVALID_REQUEST', 'Content-Length required')
        length = int(length)
        if length > MAX_BODY:
            raise ProtocolError(413, 'PAYLOAD_TOO_LARGE', 'request body exceeds 2MB')
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise ProtocolError(400, 'INVALID_REQUEST', 'body must be a JSON object') from None
        if not isinstance(data, dict):
            raise ProtocolError(400, 'INVALID_REQUEST', 'body must be a JSON object')
        return data

    def _machine(self, registry):
        header = self.headers.get('Authorization', '')
        if not header.startswith('Bearer '):
            raise ProtocolError(401, 'UNAUTHORIZED', 'bearer token required')
        machine = registry.authenticate(header[len('Bearer '):].strip())
        if machine is None:
            raise ProtocolError(401, 'UNAUTHORIZED', 'unknown machine token')
        if machine['revoked']:
            raise ProtocolError(401, 'MACHINE_REVOKED', 'token has been revoked')
        registry.touch(machine['id'], self.client_address[0],
                       self.headers.get('X-CP-Agent-Version'))
        return machine

    def _dispatch_project(self, key, verb, body, machine):
        registry = self.coordinator.registry()
        try:
            if machine is not None and verb in WRITES:
                try:
                    registry.require_write_access(machine, key)
                except RegistryError as exc:
                    status = 401 if str(exc).startswith('MACHINE_REVOKED') else 403
                    raise ProtocolError(status, str(exc), str(exc)) from None
            service = self.coordinator.service_for(key)
            try:
                return _invoke(service, verb, body, machine)
            finally:
                service.store.db.close()
        finally:
            registry.close()

    def _route_public(self, method, path):
        """Pre-auth routes (no machine token): health plus the enrollment and
        pairing handshakes. Returns True when the request was answered."""
        if method == 'GET' and path == '/health':
            self._send(200, envelope_ok({'status': 'up', 'protocol_version': PROTOCOL_VERSION,
                                         'server_time': time.time()}))
            return True
        if method != 'POST' or path not in ('/api/v1/enroll', '/api/v1/join',
                                            '/api/v1/join/poll'):
            return False
        registry = self.coordinator.registry()
        try:
            body = self._body()
            if path == '/api/v1/enroll':
                out = registry.enroll(body.get('code', ''), body.get('agent_version', ''),
                                      self.client_address[0])
                payload = out
            elif path == '/api/v1/join':
                out = registry.create_join(body.get('name', ''), body.get('secret_hash', ''),
                                           body.get('cert_fp', ''),
                                           agent_version=body.get('agent_version', ''))
                payload = {'state': out['state'], 'name': out['name'],
                           'expires_at': out['expires_at']}
            elif body.get('secret'):
                out = registry.consume_join(body.get('name', ''), body.get('secret', ''))
                payload = {'state': 'CONSUMED', 'name': out['name'], 'token': out['token']}
            else:
                payload = registry.consume_join_state(body.get('name', ''))
        except RegistryError as exc:
            self._send(200, envelope_fail(str(exc), str(exc)))
            return True
        finally:
            registry.close()
        self._send(200, envelope_ok(payload))
        return True

    def _route(self, method):
        path = self.path.split('?', 1)[0].rstrip('/') or '/'
        if self._route_public(method, path):
            return
        # Everything below requires the protocol header and a machine token.
        if self.headers.get(PROTOCOL_HEADER) != str(PROTOCOL_VERSION):
            raise ProtocolError(400, 'PROTOCOL_UNSUPPORTED',
                                'header %s must be %s' % (PROTOCOL_HEADER, PROTOCOL_VERSION))
        registry = self.coordinator.registry()
        try:
            machine = self._machine(registry)
        finally:
            registry.close()
        if method == 'GET' and path == '/api/ping':
            self._send(200, envelope_ok({'server_time': time.time(),
                                         'protocol_version': PROTOCOL_VERSION,
                                         'machine': machine['name']}))
            return
        if method == 'GET' and path == '/api/v1/ws':
            self._ws_serve(machine)
            return
        if method == 'POST' and path == '/api/v1/requests':
            registry = self.coordinator.registry()
            try:
                body = self._body()
                out = registry.create_grant_request(machine['id'], body.get('project', ''))
            except RegistryError as exc:
                self._send(200, envelope_fail(str(exc), str(exc)))
                return
            finally:
                registry.close()
            self._send(200, envelope_ok(out))
            return
        if method == 'POST' and path == '/api/v1/operator/act':
            # Operator L1 data plane (review MAJOR-3): a session process has no
            # executor — wired acts are requested here, fenced by machine token
            # + live execution token checked inside face.act.
            from ..operator import data_plane as op_data_plane
            op_data_plane.serve_act(self)
            return
        match = re.fullmatch(r'/api/projects/([^/]+)(?:/(.+))?', path)
        if not match:
            raise ProtocolError(404, 'NOT_FOUND', 'unknown route')
        key, verb = match.group(1), match.group(2) or 'status'
        if verb not in READ_VERBS and verb not in WRITES:
            raise ProtocolError(404, 'NOT_FOUND', 'unknown verb: ' + verb)
        if method != ('GET' if verb in ('status', 'list') else 'POST'):
            raise ProtocolError(405, 'METHOD_NOT_ALLOWED', 'wrong method for ' + verb)
        body = self._body() if method == 'POST' else self._query()
        result = self._dispatch_project(key, verb, body, machine)
        self._send(200, envelope_ok(result))

    def _ws_serve(self, machine):
        """Upgrade an authenticated agent GET into the command channel: a 101
        plus the frame loop, on the same socket, until either side closes.
        Registry auth already happened (and touched last_seen) in _route."""
        key = self.headers.get('Sec-WebSocket-Key')
        version = (self.headers.get('Sec-WebSocket-Version') or '').strip()
        if self.headers.get('Upgrade', '').lower() != 'websocket' or not key:
            raise ProtocolError(400, 'NOT_A_WEBSOCKET',
                                'Upgrade: websocket + Sec-WebSocket-Key required')
        if version and version != '13':
            raise ProtocolError(400, 'WS_VERSION_UNSUPPORTED',
                                'Sec-WebSocket-Version must be 13')
        dispatcher = self.coordinator.dispatcher
        conn = WsConnection(self.connection, machine, dispatcher)
        self.send_response(101)
        self.send_header('Upgrade', 'websocket')
        self.send_header('Connection', 'Upgrade')
        self.send_header('Sec-WebSocket-Accept', ws_proto.accept_key(key))
        self.end_headers()
        self.close_connection = True   # the socket belongs to the channel now
        dispatcher.register(machine, conn)
        try:
            # connect-time catch-up: hand this machine everything it is
            # granted for and that queued while it was offline
            dispatcher.dispatch(machine_id=machine['id'])
            dispatcher.dispatch_operator()
            conn.serve()
        finally:
            dispatcher.unregister(machine['id'], conn)

    def _query(self):
        query = self.path.split('?', 1)[1] if '?' in self.path else ''
        data = {}
        for part in query.split('&'):
            if '=' in part:
                k, v = part.split('=', 1)
                data[k] = v
        return data

    def do_GET(self):
        try:
            self._route('GET')
        except ProtocolError as exc:
            self._send(exc.status, envelope_fail(exc.code, exc.message))
        except ValueError as exc:  # domain rejection: CLI-shaped envelope
            self._send(200, envelope_fail(type(exc).__name__, str(exc)))
        except Exception as exc:  # noqa: BLE001 - fail-closed envelope, never a traceback page
            self._send(500, envelope_fail('INTERNAL', type(exc).__name__ + ': ' + str(exc)))

    def do_POST(self):
        try:
            self._route('POST')
        except ProtocolError as exc:
            self._send(exc.status, envelope_fail(exc.code, exc.message))
        except ValueError as exc:  # domain rejection: CLI-shaped envelope
            self._send(200, envelope_fail(type(exc).__name__, str(exc)))
        except Exception as exc:  # noqa: BLE001
            self._send(500, envelope_fail('INTERNAL', type(exc).__name__ + ': ' + str(exc)))


def _invoke_read(service, verb, body):
    if verb == 'sync':
        return service.sync()
    if verb == 'status':
        return service.status()
    return {'items': service.store.items(body.get('kind'))}


def _invoke_claim(service, body, machine):
    ttl = body.get('ttl')
    if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0):
        raise ProtocolError(400, 'INVALID_REQUEST', 'ttl must be a positive integer')
    suffix = (body.get('worker') or '').strip()
    # The enrolled machine name is the identity authority; the suffix is
    # the session/loop label. No suffix appends no label — the previous
    # fallback built '<machine><machine>' instead of a bare machine name.
    worker = machine['name'] + ('/' + suffix if suffix else '')
    execution = service.claim(body['loop'], worker, body.get('item'), ttl=ttl)
    if execution is None:
        return {'execution': None, 'item': None, 'policy': service.policy(),
                'blocked_by': service.store.last_blocked}
    item = dict(service.store.db.execute('SELECT * FROM work_items WHERE id=?',
                                         (execution['item_id'],)).fetchone())
    return {'execution': execution, 'item': item, 'policy': service.policy()}


def _invoke_round_write(service, verb, body):
    def need(*names):
        missing = [n for n in names if n not in body]
        if missing:
            raise ProtocolError(400, 'INVALID_REQUEST', 'missing fields: ' + ','.join(missing))
    if verb == 'bind':
        need('execution', 'token', 'context')
        return service.bind_checked(body['execution'], body['token'], body['context'])
    if verb == 'verify':
        need('execution', 'token', 'context', 'checks')
        return service.record_verification(body['execution'], body['token'],
                                           body['context'], body['checks'])
    need('execution', 'token', 'result')
    return service.report(body['execution'], body['token'], body['result'])


def _invoke_publish(service, verb, body):
    def need(*names):
        missing = [n for n in names if n not in body]
        if missing:
            raise ProtocolError(400, 'INVALID_REQUEST', 'missing fields: ' + ','.join(missing))
    need('execution', 'token')
    if verb == 'publish/begin':
        return service.prepare_publish(body['execution'], body['token'],
                                       context=body.get('context'))
    need('op')
    return service.complete_publish(body['execution'], body['token'], body['op'],
                                    pushed=body.get('pushed'))


def _invoke_command_ack(service, body, machine):
    if 'command' not in body or 'outcome' not in body:
        raise ProtocolError(400, 'INVALID_REQUEST', 'missing fields: command,outcome')
    outcome = body['outcome']
    if not isinstance(outcome, str):
        raise ProtocolError(400, 'INVALID_REQUEST', 'outcome must be a string')
    execution = body.get('execution')
    if execution is not None and not isinstance(execution, str):
        raise ProtocolError(400, 'INVALID_REQUEST', 'execution must be a string')
    token = body.get('token')
    if token is not None and (isinstance(token, bool) or not isinstance(token, int)):
        raise ProtocolError(400, 'INVALID_REQUEST', 'token must be an integer')
    acked = service.ack_command(body['command'], machine['name'], outcome,
                                fail_reason=body.get('fail_reason'),
                                execution=execution)
    # A command that failed AFTER its claim was taken hands the round back: the
    # ack is the ledger's business, the execution is the item's, and only the
    # holder's token can release the latter. Without this a spawn that died
    # leaves the execution RUNNING — a phantom "审核中" card and a held
    # worktree — until the lease expires hours later.
    if (acked is not None and execution and token is not None
            and outcome.upper() == 'FAILED'):
        _release_claim(service, execution, token)
    return acked


def _release_claim(service, execution, token):
    """Best effort by contract: the ack has already landed, so a refused or
    failed release (wrong token, unresolved publication) is recorded, never
    turned into an error the agent would retry."""
    try:
        service.abandon(execution, token)
    except Exception as exc:
        try:
            service.store.event('command.abandon_failed',
                                {'execution': execution,
                                 'reason': '%s: %s' % (type(exc).__name__, exc)})
        except Exception:
            pass


def _invoke_command_progress(service, body, machine):
    def need(*names):
        missing = [n for n in names if n not in body]
        if missing:
            raise ProtocolError(400, 'INVALID_REQUEST',
                                'missing fields: ' + ','.join(missing))
    need('execution', 'events')
    if not isinstance(body['events'], list):
        raise ProtocolError(400, 'INVALID_REQUEST', 'events must be a list')
    if not all(isinstance(e, dict) for e in body['events']):
        raise ProtocolError(400, 'INVALID_REQUEST', 'events must be objects')
    done = body.get('done', False)
    if not isinstance(done, bool):
        raise ProtocolError(400, 'INVALID_REQUEST', 'done must be a boolean')
    added = service.command_progress(body['execution'], body['events'],
                                     done=done)
    return {'added': added}


def _invoke(service, verb, body, machine):
    if verb == 'sync':
        return service.sync()
    if verb == 'status':
        return service.status()
    if verb == 'list':
        return {'items': service.store.items(body.get('kind'))}
    if verb == 'claim':
        return _invoke_claim(service, body, machine)
    if verb in ('bind', 'verify', 'report'):
        return _invoke_round_write(service, verb, body)
    if verb.startswith('publish/'):
        return _invoke_publish(service, verb, body)
    if verb == 'command_ack':
        return _invoke_command_ack(service, body, machine)
    if verb == 'command_progress':
        return _invoke_command_progress(service, body, machine)
    raise ProtocolError(404, 'NOT_FOUND', 'unknown verb: ' + verb)


def make_server(coordinator, host, port):
    handler = type('BoundHandler', (Handler,), {'coordinator': coordinator})
    return ThreadingHTTPServer((host, port), handler)
