#!/usr/bin/env python3
"""Management CLI for the CP server: init, serve, enroll codes, machines.

The management plane deliberately has no network endpoints — operators run it
on the server machine, next to the coordination state it controls.
"""
from __future__ import annotations

import argparse
import json
import os
from http.server import ThreadingHTTPServer
from pathlib import Path
import ssl
import stat
import sys
import threading
import time

SERVICE = Path(__file__).resolve().parents[1]
if str(SERVICE) not in sys.path:
    sys.path.insert(0, str(SERVICE))

from codeloop.cp.protocol import PROTOCOL_VERSION
from codeloop.cp.server import spawn_options
from codeloop.operator import config as operator_config
from codeloop.operator import ingest as operator_ingest
from codeloop.operator import store as operator_store
from codeloop.operator import wiring as operator_wiring
from codeloop.operator import face
from codeloop.cp.registry import Registry, RegistryError

DEFAULT_HOME = Path.home() / '.code-loop' / 'server'
DEFAULT_PORT = 8722
DEFAULT_UI_HOST = '127.0.0.1'
DEFAULT_UI_PORT = 8712
SERVER_JSON = {
    'host': '127.0.0.1',
    'port': DEFAULT_PORT,
    'projects_root': str(Path.home() / '.code-loop' / 'projects'),
    'cert_file': None,
    'key_file': None,
    'observer_period': 120,
    'ui_host': DEFAULT_UI_HOST,
    'ui_port': DEFAULT_UI_PORT,
    'ws_ping_seconds': 30,
}
DEFAULT_OBSERVER_PERIOD = SERVER_JSON['observer_period']


def load_settings(home):
    path = Path(home) / 'server.json'
    if not path.is_file():
        raise SystemExit('server.json missing under %s; run `cp_server.py init` first' % home)
    settings = json.loads(path.read_text(encoding='utf-8'))
    settings.setdefault('host', SERVER_JSON['host'])
    settings.setdefault('port', SERVER_JSON['port'])
    settings.setdefault('projects_root', SERVER_JSON['projects_root'])
    settings.setdefault('cert_file', None)
    settings.setdefault('key_file', None)
    settings.setdefault('observer_period', DEFAULT_OBSERVER_PERIOD)
    settings.setdefault('ui_host', SERVER_JSON['ui_host'])
    settings.setdefault('ui_port', SERVER_JSON['ui_port'])
    settings.setdefault('ws_ping_seconds', SERVER_JSON['ws_ping_seconds'])
    period = settings['observer_period']
    if isinstance(period, bool) or not isinstance(period, (int, float)) or period <= 0:
        raise SystemExit('server.json: observer_period must be a positive number of seconds')
    ping = settings['ws_ping_seconds']
    if isinstance(ping, bool) or not isinstance(ping, (int, float)) or ping < 5:
        raise SystemExit('server.json: ws_ping_seconds must be a number of seconds >= 5')
    return settings


def cmd_init(args):
    home = Path(args.home)
    home.mkdir(parents=True, exist_ok=True)
    server_json = home / 'server.json'
    if not server_json.is_file():
        server_json.write_text(json.dumps(SERVER_JSON, ensure_ascii=False, indent=2), encoding='utf-8')
    Registry(home / 'registry.sqlite').close()
    print(json.dumps({'ok': True, 'result': {
        'home': str(home), 'server_json': str(server_json),
        'note': 'for non-loopback serving, create a self-signed cert with openssl and set cert_file/key_file in server.json; example: openssl req -x509 -newkey rsa:2048 -nodes -keyout cp-server.key -out cp-server.crt -days 3650 -subj "/CN=cp.your.lan"',
        'protocol_version': PROTOCOL_VERSION}}, ensure_ascii=False, sort_keys=True))
    return 0


def _port_alive(host, port, timeout=2.0):
    """TCP-probe a port — a TLS server answers at the TCP level, so no
    handshake is needed."""
    import socket
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _acquire_daemon_lock(home, host, port):
    """Single-instance gate: an exclusive kernel byte-range lock on a lock
    FILE, held for the process lifetime.

    The kernel releases the lock when the process dies, so a leftover lock
    file is harmless by construction — the next starter locks it without any
    recovery protocol. Directory-based advisory locks (the previous scheme)
    cannot do this: liveness is not observable from a mkdir dir, and any
    recovery race allows stealing a peer's fresh lock (Windows also lets
    ThreadingHTTPServer double-bind via allow_reuse_address, so two racers
    would both "bind" and share one registry). Legacy mkdir-dir locks from
    older versions are migrated: port probed, then dir removed if free."""
    lock_path = Path(home) / 'daemon.lock'
    if lock_path.is_dir():
        # 旧版 mkdir 目录锁迁移：端口机器全局——127.0.0.1 与配置 host（非环回字面量）
        # 都探，任一应答即拒绝（旧版 serve 可能只绑具体 IP）；host 漂移探测落空时
        # 随后 bind 会响亮失败，不会双活。
        probe_hosts = ['127.0.0.1']
        if host_literal := (host or '').strip():
            if host_literal not in ('127.0.0.1', '::1', 'localhost'):
                probe_hosts.append(host_literal)
        if any(_port_alive(h, port) for h in probe_hosts):
            raise SystemExit(
                'DAEMON_LOCK_EXISTS: %s — port %s is in use; assume a serve '
                'instance is live and stop it first. If nothing of ours listens '
                'there, find the occupier manually — the legacy lock is kept.'
                % (lock_path, port))
        print('legacy mkdir daemon.lock with nothing on port %s — migrating to '
              'kernel file lock' % port, flush=True)
        try:
            lock_path.rmdir()
        except (FileNotFoundError, NotADirectoryError):
            pass  # 并发迁移：别的启动者已移除，落到文件锁上一决胜负
        except OSError as exc:
            raise SystemExit(
                'DAEMON_LOCK_MIGRATION: cannot remove legacy lock %s (%s) — '
                'remove it manually' % (lock_path, exc)) from None
    try:
        fh = open(lock_path, 'a+')
    except OSError as exc:
        raise SystemExit(
            'DAEMON_LOCK_UNRECOVERABLE: %s cannot be opened as the lock file (%s) '
            '— remove it manually' % (lock_path, exc)) from None
    try:
        fh.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise SystemExit(
            'DAEMON_LOCK_EXISTS: %s is held by a live serve process — stop it first'
            % lock_path) from None
    return fh


def _observer_crawl_factory(config_path):
    """Build the crawl callback for one project from its server-side config.

    The coordinator loader (no working directory, no git) is the right one
    here: the daemon may host a project whose clone lives on another machine.
    """
    from codeloop.cp.github import GitHub
    from codeloop.cp.runtime import load_server_config
    cfg = load_server_config(config_path)
    return GitHub(cfg['platform']).snapshot


def _observer_driver(settings, stop=None):
    """Keep the shared observer store fresh for every hosted project.

    Without this, the first claim after each snapshot TTL pays a full crawl
    inline; with it, claim/sync read facts that are at most one period old.
    Failures stay per project (one broken remote never starves the rest) and
    are printed once per distinct error, with a recovery line.
    """
    from codeloop.cp import observer
    period = float(settings['observer_period'])
    db_path = observer.default_db_path()
    failures = {}

    def report(key, exc):
        if exc is None:
            if failures.pop(key, None):
                print(json.dumps({'ok': True, 'result': {'observer': key, 'state': 'recovered'}},
                                 ensure_ascii=False, sort_keys=True), flush=True)
            return
        message = '%s: %s' % (type(exc).__name__, exc)
        if failures.get(key) != message:
            failures[key] = message
            print(json.dumps({'ok': False, 'code': 'OBSERVER_REFRESH',
                              'message': '%s: %s' % (key, message)},
                             ensure_ascii=False, sort_keys=True), flush=True)

    observer.run_driver(db_path, settings['projects_root'], _observer_crawl_factory,
                        period, stop=stop, report=report)


def _dash_config():
    """The machine-local dashboard.json (alerts section for the merged UI
    listener, plus the optional ``operator`` section for the in-process
    subscription). None when absent or unreadable — the UI stays silent on
    alerts and the operator subscription stays disabled."""
    config_path = Path.home() / '.code-loop' / 'dashboard.json'
    try:
        config = json.loads(config_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return config if isinstance(config, dict) else None


def _dash_alert_config():
    """The alerts section of dashboard.json (legacy alias for the merge)."""
    config = _dash_config()
    alerts = config.get('alerts') if config else None
    return alerts if isinstance(alerts, dict) else None


def _ui_listener(settings, coordinator):
    """The operator UI (the former dashboard) as a loopback listener of this
    same process — one runtime, two faces. Binding stays 127.0.0.1 (the UI is
    never LAN-exposed) and plain HTTP (the loopback-only discipline the rest
    of the codebase already enforces). The whitelisted actions become plain
    in-process calls: collect/reply/trigger need no API, no token, no IPC —
    the trigger lands on the command ledger and is pushed by this process's
    dispatcher. Returns (httpd, db) or (None, None) when the UI is disabled
    (``ui_port: null`` in server.json)."""
    from codeloop.dash import collect, serve as dash_serve, store as dash_store
    ui_port = settings.get('ui_port', DEFAULT_UI_PORT)
    if ui_port is None:
        return None, None
    ui_host = settings.get('ui_host') or DEFAULT_UI_HOST
    # The UI has no authentication of its own: its POST surface (decision
    # reply = platform write; command enqueue = execution) is protected only by
    # loopback binding plus the CSRF header. A non-loopback ui_host would
    # publish it to the LAN, so it is refused loudly instead.
    if ui_host not in ('127.0.0.1', '::1', 'localhost'):
        print(json.dumps({'ok': False, 'code': 'UI_HOST_NOT_LOOPBACK',
                          'message': 'ui_host 必须是 loopback（127.0.0.1/::1/localhost）——'
                                     'UI 无独立鉴权，不得暴露到 LAN；协调面照常服务'},
                         ensure_ascii=False, sort_keys=True), flush=True)
        return None, None
    # Windows lets ThreadingHTTPServer double-bind (allow_reuse_address), so a
    # bind success proves nothing — probe first, the same way the daemon lock
    # does (issue documented in _acquire_daemon_lock).
    if _port_alive(ui_host, ui_port):
        print(json.dumps({'ok': False, 'code': 'UI_PORT_IN_USE',
                          'message': '%s:%s 已被占用 — 协调面照常服务，但 UI 未启动；'
                                     '停掉占端口的旧 dashboard serve 后重启即可恢复 UI'
                                     % (ui_host, ui_port)},
                         ensure_ascii=False, sort_keys=True), flush=True)
        return None, None
    db_path = str(collect.DEFAULT_DB)
    root = settings['projects_root']
    alerts = _dash_alert_config()
    # Operator subscription (MAJOR-1 / spec PR1 第 2 挂点): the in-process
    # evaluation point mirrors the CLI collect path — fail-closed, off by
    # default, and the UI keeps serving when the subscription cannot start.
    on_alerts = None
    op_store = None
    try:
        from codeloop.operator import config as operator_config
        from codeloop.operator import ingest as operator_ingest
        from codeloop.operator import store as operator_store
        op_cfg = operator_config.load_operator_config(_dash_config())
        if op_cfg and op_cfg.get('enabled'):
            op_store = operator_store.open_store()
            on_alerts = (lambda fresh: operator_ingest.ingest_alerts(
                op_store, op_cfg, fresh))
    except Exception:
        on_alerts = None
        op_store = None
    db = dash_store.connect(db_path)
    # Operator assembly (2026-09-30): everything behind one machine-local
    # switch. Absent/disabled config leaves serve byte-identical to before.
    # Review MAJOR-1: operator is a *top-level* section of dashboard.json —
    # reading the alerts sub-section made op_cfg always None, so the whole
    # serve-side assembly silently never activated.
    op_cfg = operator_config.load_operator_config(_dash_config())
    op_conn = operator_store.open_store() if op_cfg and op_cfg['enabled'] else None
    on_operator_trigger = on_operator_options = None
    if op_conn is not None:
        face.wire({'collect_refresh', 'command_resend', 'wal_checkpoint',
                   'github_create_issue', 'github_comment_issue',
                   'gitea_create_issue', 'gitea_comment_issue',
                   'escalate_human'},
                  operator_wiring.make_executor(
                      root, coordinator.dispatcher,
                      dash_serve.make_refresh(db_path, root=root, config=alerts),
                      dash_db=db_path))
        coordinator.event_sink, _feed = operator_wiring.start_consumer(
            op_conn, op_cfg, coordinator.dispatcher,
            Path.home() / '.code-loop' / 'logs' / 'watchdog.jsonl', dash_db=db)
        on_operator_trigger = operator_wiring.make_manual_trigger(
            op_conn, op_cfg, coordinator.dispatcher)
        on_operator_options = operator_wiring.make_options(op_cfg, coordinator.dispatcher)
    handler = dash_serve.create(
        db, config=alerts, now=time.time,
        db_factory=lambda: dash_store.connect_readonly(db_path),
        on_collect=dash_serve.make_refresh(db_path, root=root, config=alerts,
                                           on_alerts=on_alerts),
        on_reply=dash_serve.make_replier(db_path, root=root),
        on_review_trigger=dash_serve.make_command_trigger(db_path, root=root,
                                                          coordinator=coordinator),
        on_command_status=dash_serve.make_command_status(db_path, root=root,
                                                         coordinator=coordinator),
        on_repair_trigger=dash_serve.make_command_trigger(db_path, root=root,
                                                          coordinator=coordinator,
                                                          action='repair_round'),
        on_repair_status=dash_serve.make_command_status(db_path, root=root,
                                                        coordinator=coordinator,
                                                        action='repair_round'),
        on_operator_trigger=on_operator_trigger,
        on_operator_options=on_operator_options,
        on_preflight=coordinator.dispatcher.registrations,
        on_spawn_options=lambda project='': spawn_options(
            project=project, dispatcher=coordinator.dispatcher))
    try:
        httpd = ThreadingHTTPServer((ui_host, int(ui_port)), handler)
    except OSError as exc:
        # The coordination plane must not die because the UI port is busy: serve
        # workers without the UI and say so loudly (Linux-side bind race).
        db.close()
        print(json.dumps({'ok': False, 'code': 'UI_PORT_IN_USE',
                          'message': '%s:%s (%s) — 协调面照常服务，但 UI 未启动'
                                     % (ui_host, ui_port, exc)},
                         ensure_ascii=False, sort_keys=True), flush=True)
        return None, None
    return httpd, db


def _server_local_service(project_key, settings):
    """A Service for a project this server does not expose to workers (its
    ledger still needs aging out). Server-local personality: no git, no
    working directory."""
    from codeloop.cp.runtime import load_server_config
    from codeloop.cp.service import Service
    path = Path(settings['projects_root']) / project_key / 'control-plane.json'
    try:
        cfg = load_server_config(path)
    except Exception:
        return None
    try:
        return Service(cfg, authorization={'host': 'cp-server', 'managed_by': 'registry'})
    except Exception:
        return None


def repair_tick(coordinator, settings):
    """One repair pass over every project this server holds a config for:
    age out / re-queue ledger rows, then re-offer anything this pass re-queued
    to whatever agent is online now (nothing else would — the machine that
    lost the command is gone). Dispatch itself stays event-driven; this is the
    net that catches half-delivered work. Returns the re-queued count."""
    requeued = 0
    for project_key in coordinator.all_keys():
        try:
            service = coordinator.service_for(project_key)
        except Exception:
            # not exposed to workers (its ledger still ages out)
            service = _server_local_service(project_key, settings)
        if service is None:
            continue
        try:
            requeued += service.sweep_commands().get('requeued', 0)
        except Exception:
            pass
        finally:
            try:
                service.store.db.close()
            except Exception:
                pass
    if requeued:
        try:
            coordinator.dispatcher.dispatch()
        except Exception:
            pass
    return requeued


def _repair_loop(coordinator, settings, period=60):
    while True:
        time.sleep(period)
        try:
            repair_tick(coordinator, settings)
        except Exception:
            pass   # per-project failures already stay contained


def _watch_stdin_eof(httpd):
    """Exit this serve process once stdin reaches EOF — the launcher died.

    Opt-in (``--exit-on-stdin-eof``) for spawners that hold our stdin pipe: a
    hard-killed parent runs no teardown, so without this a test-launched serve
    outlives it forever (issue #249: 35 leaked servers, two of which squatted
    the dashboard's UI port). Pipe EOF raises no error — the read simply
    returns b'' — so the launcher closing its end is the whole signal. Shutdown
    goes through the normal path (serve_forever returns, cmd_serve's finally
    releases the UI listener, its store and the daemon lock). The scheduled
    task that runs the production daemon holds no such pipe and does not pass
    the flag, so daemon behavior is unchanged.

    The flag is validated up front by ``_require_stdin_pipe``: without that
    check a launcher that passes the flag but hands over DEVNULL or an already
    closed stdin would make serve print its success banner and then exit 0
    within seconds — a silent outage that looks exactly like the leak this
    guards against.
    """
    def watch():
        while True:
            try:
                chunk = os.read(0, 1)
            except OSError:
                break
            if not chunk:
                break
        httpd.shutdown()
    threading.Thread(target=watch, name='stdin-eof', daemon=True).start()


def _require_stdin_pipe():
    """Fail fast when --exit-on-stdin-eof has no pipe to watch.

    stdin must be a real pipe whose write end the launcher holds; a character
    device (DEVNULL, console) or an already-closed stream carries no such
    signal, so the watcher would read EOF immediately and shut the server down
    silently. A fail-open watcher is just as bad: with no stdin at all the
    guard would never fire and the leak would return unnoticed.
    """
    try:
        mode = os.fstat(0).st_mode
    except OSError as exc:
        raise SystemExit('EXIT_ON_STDIN_EOF_REQUIRES_PIPE: stdin is unavailable (%s); '
                         'spawn serve with a pipe on stdin or drop the flag' % exc) from None
    if not stat.S_ISFIFO(mode):
        raise SystemExit('EXIT_ON_STDIN_EOF_REQUIRES_PIPE: stdin is not a pipe; '
                         'spawn serve with stdin=PIPE (the launcher closing its end is '
                         'the exit signal) or drop the flag')


def cmd_serve(args):
    settings = load_settings(args.home)
    from codeloop.cp import server as cp_server_module
    from codeloop.cp.server import Coordinator, make_server
    if args.exit_on_stdin_eof:
        _require_stdin_pipe()
    host = args.host or settings['host']
    port = args.port or settings['port']
    cert = settings.get('cert_file')
    key = settings.get('key_file')
    if (cert or key) and not (cert and key):
        raise SystemExit('server.json: cert_file and key_file must be set together')
    cp_server_module.WS_PING_SECONDS = float(settings['ws_ping_seconds'])
    coordinator = Coordinator(settings['projects_root'], Path(args.home) / 'registry.sqlite')
    lock_fh = _acquire_daemon_lock(args.home, host, port)
    ui = None
    try:
        # Only the lock holder refreshes: a second serve process must not put
        # a second crawler on the same store.
        threading.Thread(target=_observer_driver, args=(settings,),
                         name='observer', daemon=True).start()

        threading.Thread(target=_repair_loop, args=(coordinator, settings),
                         name='command-repair', daemon=True).start()
        ui, ui_db = _ui_listener(settings, coordinator)
        if ui is not None:
            threading.Thread(target=ui.serve_forever, name='ui', daemon=True).start()
        httpd = make_server(coordinator, host, port)
        if cert:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        print(json.dumps({'ok': True, 'result': {
            'listening': '%s:%d' % (host, port),
            'ui': ('%s:%d' % (ui.server_address[0], ui.server_address[1])) if ui else None,
            'tls': bool(cert),
            'protocol_version': PROTOCOL_VERSION}},
            ensure_ascii=False, sort_keys=True), flush=True)
        if args.exit_on_stdin_eof:
            _watch_stdin_eof(httpd)
        httpd.serve_forever()
    finally:
        if ui is not None:
            ui.server_close()
            ui_db.close()
        lock_fh.close()
    return 0


def cmd_enroll_code(args):
    registry = Registry(Path(args.home) / 'registry.sqlite')
    try:
        code = registry.create_enroll_code(args.name, args.project, ttl=args.ttl)
    except RegistryError as exc:
        raise SystemExit(str(exc)) from None
    finally:
        registry.close()
    print(json.dumps({'ok': True, 'result': {'code': code, 'name': args.name,
                                             'projects': args.project, 'ttl': args.ttl}},
                     ensure_ascii=False, sort_keys=True))
    return 0


def cmd_machines(args):
    registry = Registry(Path(args.home) / 'registry.sqlite')
    try:
        if args.action == 'list':
            result = {'machines': registry.list_machines()}
        elif args.action == 'enable':
            registry.set_enabled(args.name, True)
            result = {'name': args.name, 'enabled': True}
        elif args.action == 'disable':
            registry.set_enabled(args.name, False)
            result = {'name': args.name, 'enabled': False}
        elif args.action == 'revoke':
            registry.revoke(args.name)
            result = {'name': args.name, 'revoked': True}
        elif args.action == 'rotate':
            token = registry.rotate(args.name)
            result = {'name': args.name, 'token': token}
        elif args.action == 'grant':
            registry.grant(args.name, args.project)
            result = {'name': args.name, 'granted': args.project}
        else:  # ungrant
            registry.ungrant(args.name, args.project)
            result = {'name': args.name, 'ungranted': args.project}
    except RegistryError as exc:
        raise SystemExit(str(exc)) from None
    finally:
        registry.close()
    print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False, sort_keys=True))
    return 0


def _server_cert_fp(home):
    """sha256 fingerprint of this server's own cert (from server.json), or None
    when serving without TLS — approve then skips the fingerprint compare."""
    settings = load_settings(home)
    cert = settings.get('cert_file')
    if not cert:
        return None
    path = Path(cert)
    if not path.is_file():
        raise SystemExit('server.json cert_file %s missing' % cert)
    import hashlib
    import ssl
    der = ssl.PEM_cert_to_DER_cert(path.read_text(encoding='utf-8'))
    return hashlib.sha256(der).hexdigest()


def cmd_joins(args):
    registry = Registry(Path(args.home) / 'registry.sqlite')
    try:
        if args.action == 'pending':
            result = {'joins': registry.list_joins()}
        elif args.action == 'approve':
            out = registry.approve_join(args.name, args.project, as_name=args.as_name,
                                        server_cert_fp=_server_cert_fp(args.home))
            try:
                projects = json.loads(out.get('projects') or '[]')
            except ValueError:
                projects = []
            result = {'name': args.name, 'state': out['state'],
                      'approved_as': out.get('approved_name') or args.name,
                      'projects': projects,
                      'fingerprint': _server_cert_fp(args.home)}
        else:  # reject
            registry.reject_join(args.name)
            result = {'name': args.name, 'state': 'REJECTED'}
    except RegistryError as exc:
        raise SystemExit(str(exc)) from None
    finally:
        registry.close()
    print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False, sort_keys=True))
    return 0


def _exposed_projects(home):
    """(projects_root, [exposed keys]) — the scan behind `projects`, reused by
    `requests approve` to refuse proposals for projects this server won't serve."""
    settings = load_settings(home)
    root = Path(settings['projects_root'])
    exposed = []
    if root.is_dir():
        for path in sorted(root.glob('*/control-plane.json')):
            try:
                cfg = json.loads(path.read_text(encoding='utf-8'))
            except ValueError:
                continue
            if cfg.get('remote') is True:
                exposed.append(path.parent.name)
    return root, exposed


def cmd_projects(args):
    root, exposed = _exposed_projects(args.home)
    print(json.dumps({'ok': True, 'result': {'projects_root': str(root), 'exposed': exposed}},
                     ensure_ascii=False, sort_keys=True))
    return 0


def cmd_requests(args):
    registry = Registry(Path(args.home) / 'registry.sqlite')
    try:
        if args.action == 'pending':
            result = {'requests': registry.list_grant_requests()}
        elif args.action == 'approve':
            row = registry._grant_request_row(args.id)  # any state: registry owns the
            if row is None:                             # NOT_FOUND/NOT_PENDING wording
                raise SystemExit('REQUEST_NOT_FOUND: no grant request ' + str(args.id))
            project = row['org_repo']
            root, exposed = _exposed_projects(args.home)
            if project not in exposed:
                raise SystemExit(
                    'PROJECT_NOT_EXPOSED: %s has no remote:true config under %s — '
                    'expose the project first' % (project, root))
            out = registry.approve_grant_request(args.id)
            result = {'id': args.id, 'state': out['state'],
                      'machine': out['machine_name'], 'granted': out['org_repo']}
        else:  # reject
            registry.reject_grant_request(args.id)
            result = {'id': args.id, 'state': 'REJECTED'}
    except RegistryError as exc:
        raise SystemExit(str(exc)) from None
    finally:
        registry.close()
    print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False, sort_keys=True))
    return 0


def parser():
    p = argparse.ArgumentParser(prog='cp_server.py')
    p.add_argument('--home', default=str(DEFAULT_HOME), help='server home (default ~/.code-loop/server)')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('init')
    serve = sub.add_parser('serve')
    serve.add_argument('--host')
    serve.add_argument('--port', type=int)
    serve.add_argument('--exit-on-stdin-eof', action='store_true',
                       help='exit when stdin reaches EOF (the launching process closed its '
                            'pipe end, e.g. a test run that was killed). Requires the launcher '
                            'to spawn us with a pipe on stdin; do not pass it to the daemon.')
    enroll = sub.add_parser('enroll-code')
    enroll.add_argument('--name', required=True)
    enroll.add_argument('--project', action='append', default=[])
    enroll.add_argument('--ttl', type=int, default=600)
    machines = sub.add_parser('machines')
    machines.add_argument('action', choices=('list', 'enable', 'disable', 'revoke', 'rotate',
                                             'grant', 'ungrant'))
    machines.add_argument('--name')
    machines.add_argument('--project')
    joins = sub.add_parser('joins')
    joins.add_argument('action', choices=('pending', 'approve', 'reject'))
    joins.add_argument('--name')
    joins.add_argument('--project', action='append', default=[],
                       help='optional grants to attach at approval; projects can also be '
                            'proposed by the worker (requests) or granted later (machines)')
    joins.add_argument('--as-name', help='approved machine name (default: requested name)')
    requests = sub.add_parser('requests')
    requests.add_argument('action', choices=('pending', 'approve', 'reject'))
    requests.add_argument('--id', help='grant request id (see requests pending)')
    sub.add_parser('projects')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == 'init':
        return cmd_init(args)
    if args.command == 'serve':
        return cmd_serve(args)
    if args.command == 'enroll-code':
        return cmd_enroll_code(args)
    if args.command == 'machines':
        if args.action in ('enable', 'disable', 'revoke', 'rotate') and not args.name:
            raise SystemExit('--name is required for %s' % args.action)
        if args.action in ('grant', 'ungrant') and not (args.name and args.project):
            raise SystemExit('--name and --project are required for %s' % args.action)
        return cmd_machines(args)
    if args.command == 'joins':
        if args.action in ('approve', 'reject') and not args.name:
            raise SystemExit('--name is required for %s' % args.action)
        return cmd_joins(args)
    if args.command == 'requests':
        if args.action in ('approve', 'reject') and not args.id:
            raise SystemExit('--id is required for %s' % args.action)
        return cmd_requests(args)
    if args.command == 'projects':
        return cmd_projects(args)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
