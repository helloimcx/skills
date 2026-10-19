"""HTTP facade: serves the app (the frontend/ shell + whitelisted static
assets: the app css/js and vendored libraries)
and JSON views. The merged serve process (``cp_server.py serve``) mounts this
handler on its loopback UI listener; there is no standalone dashboard server
anymore.

The handler renders derived views from an open store connection and exposes
three whitelisted POST actions: /api/collect (local refresh, no platform
write), /api/reply (post a decision reply comment to Gitea — the dashboard's
only platform write, scoped to comment_issue) and /api/review/trigger (one
`review_round` command on the project's ledger, dispatched to a connected
worker agent in the same call — no platform write here either). `now` is
injectable for deterministic tests. The listener is loopback-only; all POST
routes require the custom X-Requested-With header so a random cross-origin
page cannot trigger them.
"""
import gzip
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import collect, historyapi, notify, store
from .runapi import AGENT_RUN_ID_RE, EXEC_ID_RE, run_detail
from ..cp.commands import validated_spawn_options
from ..cp.github import GitHub
Gitea = GitHub
from ..cp.protocol import ProtocolError
from ..cp.service import Service

FRONTEND_DIR = Path(__file__).parents[2] / 'frontend'   # services/code-loop/frontend
APP_HTML = FRONTEND_DIR / 'index.html'
DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8712

REPLY_BODY_MAX = 20000
REVIEW_TRIGGER_BODY_MAX = 2000


class ActionError(Exception):
    """A whitelisted POST action refusing with a specific HTTP status."""

    def __init__(self, status, error):
        super().__init__(error)
        self.status = status
        self.error = error

# GET /api/* keeps a last-good snapshot: a cached response is served at once and
# recomputed in the background (single flight per key) once it is older than
# this window — first paint never waits behind a slow query or a collect write
# lock (issue #130). A failed recompute keeps the old value. The cache is
# bounded: the frontend uses a fixed URL set, the cap only guards against
# unbounded query variants on a long-lived process.
SNAPSHOT_REFRESH_SECONDS = 10
SNAPSHOT_MAX_ENTRIES = 128

# Frontend assets served verbatim; the whitelist is the path-traversal defense
# (anything outside it 404s). No CDN: the dashboard must work offline.
STATIC_DIR = FRONTEND_DIR
STATIC_FILES = {
    '/static/app.css': ('app.css', 'text/css'),
    '/static/app.js': ('app.js', 'application/javascript'),
    '/static/marked.min.js': ('vendor/marked.min.js', 'application/javascript'),
    '/static/purify.min.js': ('vendor/purify.min.js', 'application/javascript'),
}


def _items(db):
    issues = [dict(r) for r in db.execute(
        'SELECT i.*, pr.org_repo FROM issues i JOIN projects pr ON pr.id=i.project_id '
        'ORDER BY pr.org_repo, i.number')]
    prs = []
    rows = [dict(r) for r in db.execute(
        'SELECT p.*, pr.org_repo FROM prs p JOIN projects pr '
        'ON pr.id=p.project_id ORDER BY pr.org_repo, p.number')]
    verdicts = store.verdicts_for(db, rows)  # one scan per project (issue #130)
    for pr in rows:
        verdict = store.latest_verdict(db, pr['project_id'], pr['number'],
                                       cp_verdicts=verdicts[pr['project_id']])
        pr['verdict'] = verdict[0] if verdict else None
        pr['verdict_at'] = verdict[1] if verdict else None
        prs.append(pr)
    return {'issues': issues, 'prs': prs}


def _runtime(db, org_repo):
    projects = {r['org_repo']: r['id'] for r in db.execute('SELECT id, org_repo FROM projects')}
    pid = projects.get(org_repo)
    if pid is None:
        return None
    tables = ('work_items', 'executions', 'leases', 'outbox', 'cp_events')
    data = {}
    for table in tables:
        rows = [dict(r) for r in db.execute(
            'SELECT * FROM %s WHERE project_id=? ORDER BY id DESC LIMIT 200' % table, (pid,))]
        data[table] = rows
    return data


def _known_project(db, org_repo):
    return db.execute('SELECT 1 FROM projects WHERE org_repo=?',
                      (org_repo,)).fetchone() is not None


def _project_scope(db, query):
    """(known, project) for the project-scoped GET endpoints: the scoped
    project param or None; ``known`` is False when the project is unknown."""
    project = query.get('project') or None
    return not (project is not None and not _known_project(db, project)), project


def _api_runtime(db, query):
    data = _runtime(db, query.get('project', ''))
    if data is None:
        return 'application/json', 404, {'error': 'unknown project'}
    return 'application/json', 200, data


def _api_run(db, query):
    """GET /api/run — the run-detail view's data endpoint (see runapi). The
    exec id has two shapes: a 32-hex loop execution (project-scoped) and an
    agent-run archive id (``platform:session``, project-independent — the
    project lookup may legitimately miss). The raw id reaches run_detail
    untouched: archive ids embed case-sensitive tool_use ids, so only the
    32-hex shape is case-normalized."""
    raw_exec = query.get('exec') or ''
    is_agent = bool(AGENT_RUN_ID_RE.match(raw_exec))
    if not is_agent and not EXEC_ID_RE.match(raw_exec.lower()):
        return 'application/json', 400, {'error': 'invalid execution id'}
    row = None
    if not is_agent:
        row = db.execute(
            'SELECT id, config_path, repo_cwd FROM projects WHERE org_repo=?',
            (query.get('org', ''),)).fetchone()
        if row is None:
            return 'application/json', 404, {'error': 'unknown project'}
    since = int(query.get('since') or 0)   # ValueError -> 400 via do_GET
    detail = run_detail(db, row, raw_exec if is_agent else raw_exec.lower(), since)
    if detail is None:
        return 'application/json', 404, {'error': 'unknown execution'}
    return 'application/json', 200, detail


def route(db, path, query, config=None, now=None):
    """(content_type, status, payload); payload dict => JSON, bytes => raw."""
    moment = now() if callable(now) else now
    if path in ('/', '/index.html', '/app.html'):
        return 'text/html; charset=utf-8', 200, APP_HTML.read_bytes()
    if path in STATIC_FILES:
        name, content_type = STATIC_FILES[path]
        return content_type, 200, (STATIC_DIR / name).read_bytes()
    if path == '/api/overview':
        return 'application/json', 200, store.overview(db, now=moment, config=config)
    if path == '/api/workers':
        return 'application/json', 200, store.worker_summary(db, now=moment, config=config)
    if path == '/api/queue':
        return 'application/json', 200, {
            'merges': store.pending_merges(db, now=moment),
            'decisions': store.pending_decisions(db, now=moment),
            'exceptions': store.exceptions(db, now=moment)}
    if path == '/api/issue':
        # int() raises ValueError on missing/non-numeric input -> 400 via do_GET.
        detail = store.issue_detail(db, query.get('project') or '',
                                    int(query.get('number', '')))
        if detail is None:
            return 'application/json', 404, {'error': 'unknown issue'}
        return 'application/json', 200, detail
    if path == '/api/items':
        return 'application/json', 200, _items(db)
    if path == '/api/trends':
        days = max(1, min(90, int(query.get('days', 14))))
        known, project = _project_scope(db, query)
        if not known:
            return 'application/json', 404, {'error': 'unknown project'}
        return 'application/json', 200, store.trends(db, days=days, now=moment,
                                                     org_repo=project)
    if path == '/api/kanban':
        known, project = _project_scope(db, query)
        if not known:
            return 'application/json', 404, {'error': 'unknown project'}
        return 'application/json', 200, store.kanban(db, org_repo=project, now=moment)
    if path == '/api/events':
        known, project = _project_scope(db, query)
        if not known:
            return 'application/json', 404, {'error': 'unknown project'}
        days = max(1, min(30, int(query.get('days', 7))))
        return 'application/json', 200, store.events(db, org_repo=project, days=days,
                                                     now=moment, kind=query.get('kind'))
    if path == '/api/runtime':
        return _api_runtime(db, query)
    if path == '/api/run':
        return _api_run(db, query)
    if path == '/api/history':
        return 'application/json', 200, historyapi.history(db, query)
    if path == '/api/operator':
        return 'application/json', 200, operator_ledger(db, query)
    return 'application/json', 404, {'error': 'not found'}


def operator_ledger(db, query=None):
    """The interventions ledger: operator mirror rows joined into per-round
    rows (signal → runs), newest first. Bounded (review MINOR on unbounded
    growth): the signals read is capped at `limit` (`?limit=`, default 50,
    max 500) with the full count reported as `total`; runs are read only for
    the signals in that page. The operator store stays the source of truth."""
    import json as _json
    query = query or {}
    try:
        limit = max(1, min(500, int(query.get('limit', 50))))
    except (TypeError, ValueError):
        limit = 50
    total = db.execute('SELECT count(*) FROM operator_signals').fetchone()[0]
    signals = db.execute('SELECT * FROM operator_signals '
                         'ORDER BY created_at DESC, id DESC LIMIT ?',
                         (limit,)).fetchall()
    runs = {}
    if signals:
        ids = [s['id'] for s in signals]
        marks = ','.join('?' * len(ids))
        for row in db.execute(
                'SELECT * FROM operator_runs WHERE signal_id IN (%s) '
                'ORDER BY started_at' % marks, ids):
            runs.setdefault(row['signal_id'], []).append(dict(row))
    rows = []
    for s in signals:
        signal = dict(s)
        payload = _json.loads(signal['payload'])
        out = {'id': signal['id'],
               'signal': {'source': signal['source'], 'kind': signal['kind'],
                          'key': signal['key'], 'org_repo': signal['org_repo'],
                          'severity': payload.get('severity'),
                          'message': payload.get('message'),
                          'note': payload.get('note'),
                          'received_at': signal['created_at']},
               'state': signal['state'], 'rounds': runs.get(signal['id'], [])}
        run = out['rounds'][-1] if out['rounds'] else None
        if run is not None:
            result = _json.loads(run['result']) if run['result'] else {}
            out.update({'agent': run.get('agent'), 'model': run.get('model'),
                        'started_at': run['started_at'], 'ended_at': run.get('ended_at'),
                        'run_state': run['state'],
                        'outcome': result.get('outcome'),
                        'diagnosis': result.get('diagnosis'),
                        'actions': result.get('actions', []),
                        'evidence': result.get('evidence', [])})
        else:
            out.update({'agent': None, 'model': None, 'started_at': None,
                        'ended_at': None, 'run_state': None, 'outcome': None,
                        'diagnosis': None, 'actions': [], 'evidence': []})
        rows.append(out)
    return {'ok': True, 'total': total, 'rows': rows}


def create(db, config=None, now=None, on_collect=None, on_reply=None,
           on_review_trigger=None, on_command_status=None, on_preflight=None,
           on_spawn_options=None, on_repair_trigger=None, on_repair_status=None,
           on_operator_trigger=None, on_operator_options=None):
    """Bind a handler class to an open store connection. `on_collect` (optional)
    powers POST /api/collect — a local read-from-platform refresh, never a
    platform write. `on_reply` (optional) powers POST /api/reply — the
    dashboard's single platform write (comment_issue), which also records the
    reply so the decision leaves the queue. `on_review_trigger` (optional)
    powers POST /api/review/trigger — one `review_round` command on the
    project's ledger (idempotent while live, dispatched to a connected worker
    agent in the same call), again no platform write; `on_command_status`
    (optional) powers the read-only GET /api/review/trigger/status;
    `on_repair_trigger` / `on_repair_status` power the repair_round twins at
    /api/repair/trigger and /api/repair/trigger/status — the same contract
    re-pointed at the conflict-repair command;
    `on_spawn_options` (optional) powers the read-only GET /api/spawn/options —
    the model list behind the trigger dialog, served from this host's own CLI
    detection. All require
    the custom
    X-Requested-With header so a random cross-origin page cannot trigger them
    (custom headers force CORS preflight). GET /api/* responses are cached as
    last-good snapshots (served immediately, refreshed in the background once
    stale); all POST actions wipe the snapshot so their effect is
    visible on the very next fetch. /api/collect additionally submits its
    crawl to a background single-flight thread (start_collect) — the wipe
    happens when that thread completes."""

    # Path → closure maps: one body-parsing/error-mapping block serves every
    # trigger flavor, so a new command kind is a map entry, not a copied block.
    trigger_paths = {'/api/review/trigger': (on_review_trigger,
                                             'review trigger not wired on this server'),
                     '/api/repair/trigger': (on_repair_trigger,
                                             'repair trigger not wired on this server'),
                     '/api/operator/trigger': (on_operator_trigger,
                                               'operator trigger not wired on this server')}
    status_paths = {'/api/review/trigger/status': on_command_status,
                    '/api/repair/trigger/status': on_repair_status}

    cache = {}          # key -> (content_type, status, body, fetched_at)
    cache_lock = threading.Lock()
    inflight = set()    # keys with a background recompute running
    generation = [0]    # bumped on POST cache.clear(); in-flight recomputes
                        # from an older generation must not refill the cache
                        # with pre-POST data (issue #167)

    def moment():
        return now() if callable(now) else time.time()

    # issue #314: the collection runs on a background single-flight thread.
    # Inline it used to hold the request thread for minutes (GIL + sqlite
    # while crawling), starving every other thread of the merged process —
    # the 8722 control plane included — which was the kanban's periodic
    # whole-page freeze and silently dropped clicks.
    collect_state = {'running': False, 'since': None, 'last_error': None}
    collect_lock = threading.Lock()

    def start_collect(only):
        """Submit the collection and answer at once: ``started`` (runner
        spawned) or ``skipped`` (one is already in flight). Completion still
        wipes the snapshot (generation++ + cache.clear()) so fresh facts show
        on the next fetch; a failure keeps the last-good snapshot untouched
        and lands the reason in collect_state.last_error."""
        with collect_lock:
            if collect_state['running']:
                return {'collect': {'skipped': 'collect in progress'}}
            collect_state.update(running=True, since=moment(), last_error=None)

        def run():
            try:
                on_collect(only)
            except Exception as exc:
                with collect_lock:
                    collect_state.update(running=False, last_error=str(exc))
                return
            with cache_lock:
                generation[0] += 1
                cache.clear()
            # running 置 False 放在清快照之后：轮询到 idle 即完成栅栏，
            # 提交方随后一次 refresh 必然拿到采集后的新数据。
            with collect_lock:
                collect_state['running'] = False
        try:
            threading.Thread(target=run, daemon=True).start()
        except Exception as exc:   # 线程资源耗尽等：复位单飞，别永久卡 skipped
            with collect_lock:
                collect_state.update(running=False, last_error=str(exc))
            return {'collect': {'skipped': 'spawn failed'}}
        return {'collect': {'started': True, 'project': only}}

    def recompute(key):
        gen = generation[0]
        try:
            url = urlparse(key)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            content_type, status, payload = route(db, url.path, query, config, now)
            if status == 200:
                body = payload if isinstance(payload, bytes) else json.dumps(
                    payload, ensure_ascii=False, default=str).encode('utf-8')
                with cache_lock:
                    if gen == generation[0]:
                        cache[key] = (content_type, status, body, moment())
        except Exception as exc:
            # last-good stays; surface the reason for operators, then the next
            # access retries the recompute
            print('dashboard snapshot refresh failed for %s: %r' % (key, exc),
                  file=sys.stderr)
        finally:
            with cache_lock:
                inflight.discard(key)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _accepts_gzip(self):
            return 'gzip' in (self.headers.get('Accept-Encoding') or '').lower()

        def _write_body(self, status, content_type, body):
            """Send a response body, gzip-compressed when the client accepts it
            (issue #159, user decision 1A 2A). The dashboard's read endpoints
            ship multi-MB JSON (/api/items 3.1MB, /api/kanban 765KB); gzip
            cuts the wire payload ~10x with zero API change. Compression is
            per-response on the already-rendered body — no shared state, safe
            under the threaded server."""
            if self._accepts_gzip() and len(body) >= 512:
                body = gzip.compress(body)
                encoding = 'gzip'
            else:
                encoding = None
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            if encoding:
                self.send_header('Content-Encoding', encoding)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8')
            self._write_body(status, 'application/json', body)

        def _reject(self):
            self.send_response(405)
            self.end_headers()

        do_PUT = do_DELETE = do_PATCH = _reject

        def do_POST(self):
            url = urlparse(self.path)
            if url.path not in ('/api/collect', '/api/reply') \
                    and url.path not in trigger_paths:
                self._send_json(404, {'error': 'not found'})
                return
            if self.headers.get('X-Requested-With') != 'dashboard-fetch':
                self._send_json(403, {'error': 'missing X-Requested-With header'})
                return
            if url.path == '/api/reply':
                if on_reply is None:
                    self._send_json(503, {'error': 'reply not wired on this server'})
                    return
                try:
                    length = int(self.headers.get('Content-Length', 0) or 0)
                    if length > REPLY_BODY_MAX:
                        self._send_json(400, {'error': 'reply body too long'})
                        return
                    payload = json.loads(self.rfile.read(length) or b'{}')
                except (ValueError, OSError):
                    self._send_json(400, {'error': 'invalid JSON body'})
                    return
                if not isinstance(payload, dict):
                    self._send_json(400, {'error': 'JSON body must be an object'})
                    return
                project = payload.get('project')
                number = payload.get('number')
                body = payload.get('body')
                if not isinstance(project, str) or type(number) is not int or number <= 0:
                    self._send_json(400, {'error': 'project and positive number required'})
                    return
                row = db.execute('SELECT id FROM projects WHERE org_repo=?',
                                 (project,)).fetchone()
                if row is None:
                    self._send_json(404, {'error': 'unknown project'})
                    return
                issue = db.execute('SELECT state FROM issues WHERE project_id=? AND number=?',
                                   (row['id'], number)).fetchone()
                if issue is None or issue['state'] != 'open':
                    self._send_json(409, {'error': 'issue not open'})
                    return
                if not isinstance(body, str) or not body.strip():
                    self._send_json(400, {'error': 'empty reply body'})
                    return
                if len(body) > REPLY_BODY_MAX:
                    self._send_json(400, {'error': 'reply body too long'})
                    return
                try:
                    receipt = on_reply(project, number, body.strip())
                except Exception as exc:
                    self._send_json(502, {'error': 'platform write failed: %s' % exc})
                    return
                with cache_lock:
                    generation[0] += 1
                    cache.clear()   # the reply must show on the very next fetch
                self._send_json(200, {'ok': True, 'comment_id': receipt.get('id')})
                return
            if url.path in trigger_paths:
                wired, not_wired = trigger_paths[url.path]
                if wired is None:
                    self._send_json(503, {'error': not_wired})
                    return
                try:
                    length = int(self.headers.get('Content-Length', 0) or 0)
                    if length > REVIEW_TRIGGER_BODY_MAX:
                        self._send_json(400, {'error': 'body too large'})
                        return
                    payload = json.loads(self.rfile.read(length) or b'{}')
                except (ValueError, OSError):
                    self._send_json(400, {'error': 'invalid JSON body'})
                    return
                if not isinstance(payload, dict):
                    self._send_json(400, {'error': 'JSON body must be an object'})
                    return
                project = payload.get('project')
                number = payload.get('number')
                agent = payload.get('agent')
                model = payload.get('model')
                note = payload.get('note')
                if not isinstance(project, str) or not project:
                    self._send_json(400, {'error': 'project required'})
                    return
                if number is not None and (type(number) is not int or number <= 0):
                    self._send_json(400, {'error': 'number must be a positive integer'})
                    return
                # shape first (str or absent): the whitelist lives one layer
                # down, on the closure that owns the ledger
                for name, value in (('agent', agent), ('model', model), ('note', note)):
                    if value is not None and not isinstance(value, str):
                        self._send_json(400, {'error': '%s must be a string' % name})
                        return
                try:
                    # note is operator-path only; review/repair closures are the
                    # 4-arg contract (they never see it: the forward below
                    # applies only to the operator trigger_path).
                    if url.path == '/api/operator/trigger':
                        result = wired(project, number, agent, model, note or '')
                    else:
                        result = wired(project, number, agent, model)
                except ActionError as exc:
                    self._send_json(exc.status, {'error': exc.error})
                    return
                except TypeError as exc:
                    # a 4-arg closure wired to the operator path must not 502
                    # on the 5th arg; the endpoint tests assert the contract.
                    self._send_json(502, {'error': 'operator trigger wired '
                                                   'with 4-arg closure: %s' % exc})
                    return
                except Exception as exc:
                    self._send_json(502, {'error': 'trigger failed: %s' % exc})
                    return
                with cache_lock:
                    generation[0] += 1
                    cache.clear()   # the pre-claimed round must show on the next fetch
                self._send_json(200, result)
                return
            if on_collect is None:
                self._send_json(503, {'error': 'collect not wired on this server'})
                return
            params = parse_qs(url.query)
            only = params.get('project', [None])[0]
            if only and not _known_project(db, only):
                self._send_json(404, {'error': 'unknown project'})
                return
            self._send_json(200, start_collect(only))

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == '/api/collect/status':
                # issue #314 completion polling: live counters, never the
                # snapshot cache — a stale running=False would hang the client.
                self._send_json(200, collect_state)
                return
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            key = url.path + ('?' + url.query if url.query else '')
            # /api/run 绕过快照缓存（Spec Scope）：2.5s 增量轮询要低时延，
            # 且每个 since 分片一个 key，进缓存会周期性挤掉共享的 last-good。
            # fresh=1 是操作者动作（点击触发后的那次重取）的一次性强制刷新：刚写下的
            # 台账状态必须当场可见，不能被 10s 窗口的旧体挡回。该请求既不读也不写
            # 快照，所以不会污染共享的 last-good（issue #130 的快照只服务首屏）。
            cached = url.path.startswith('/api/') and url.path not in (
                '/api/run', '/api/history', '/api/review/preflight',
                '/api/spawn/options') \
                and query.get('fresh') != '1'
            if cached:
                with cache_lock:
                    hit = cache.get(key)
                    if (hit is not None
                            and moment() - hit[3] >= SNAPSHOT_REFRESH_SECONDS
                            and key not in inflight):
                        inflight.add(key)
                        threading.Thread(target=recompute, args=(key,),
                                         daemon=True).start()
                if hit is not None:
                    content_type, status, body, _ = hit
                    self._write_body(status, content_type, body)
                    return
            if url.path == '/api/review/preflight':
                # registration preflight for the trigger buttons: which
                # connected agents hold a registration per project. Short-lived
                # liveness data — served fresh, never from the snapshot cache.
                wired = getattr(type(self), 'preflight', on_preflight)
                if wired is None:
                    self._send_json(503, {'error': 'preflight not wired on this server'})
                    return
                keys = [k for k in (query.get('projects') or '').split(',') if k]
                if not keys:
                    self._send_json(400, {'error': 'projects required'})
                    return
                try:
                    self._send_json(200, wired(keys))
                except Exception as exc:
                    self._send_json(502, {'error': 'preflight failed: %s' % exc})
                return
            if url.path in status_paths:
                wired = status_paths[url.path]
                if wired is None:
                    self._send_json(503, {'error': 'command status not wired here'})
                    return
                try:
                    result = wired(query.get('project', ''),
                                   int(query.get('number', '')))
                except (ValueError, KeyError) as exc:
                    self._send_json(400, {'error': str(exc)})
                    return
                self._send_json(200, result)
                return
            if url.path == '/api/spawn/options':
                if on_spawn_options is None:
                    self._send_json(503,
                                    {'error': 'spawn options not wired on this server'})
                    return
                try:
                    self._send_json(200, on_spawn_options(
                        project=query.get('project', '')))
                except Exception as exc:
                    self._send_json(500, {'error': 'spawn options failed: %s' % exc})
                return
            if url.path == '/api/operator/options':
                if on_operator_options is None:
                    self._send_json(503,
                                    {'error': 'operator not wired on this server'})
                    return
                try:
                    self._send_json(200, on_operator_options())
                except Exception as exc:
                    self._send_json(500, {'error': 'operator options failed: %s' % exc})
                return
            try:
                content_type, status, payload = route(db, url.path, query, config, now)
            except (ValueError, KeyError) as exc:
                content_type, status = 'application/json', 400
                payload = {'error': str(exc)}
            body = payload if isinstance(payload, bytes) else json.dumps(
                payload, ensure_ascii=False, default=str).encode('utf-8')
            if cached and status == 200:
                with cache_lock:
                    if len(cache) >= SNAPSHOT_MAX_ENTRIES:
                        cache.clear()   # bound memory: drop all, they re-fill on access
                    cache[key] = (content_type, status, body, moment())
            self._write_body(status, content_type, body)

    Handler.snapshot_cache = cache   # test/introspection surface
    return Handler


def make_refresh(db_path, root=None, config=None, on_alerts=None):
    """Build a collect+notify closure with its own short-lived connection, so
    it can run on the listener's request thread or a background loop.
    `only` (org__repo from POST /api/collect?project=) scopes to one project.
No process lock: concurrent collectors are safe on the store itself (see
    collect.collect). on_alerts (optional) is handed the fresh alert list at
    the evaluation point (operator subscription, same contract as the CLI
    collect path); its failures are recorded by push_new_alerts, never raised."""
    collect_root = root or collect.DEFAULT_ROOT

    def refresh_once(only=None):
        db = store.connect(db_path)
        try:
            summary = collect.collect(db, root=collect_root,
                                      only={only} if only else None)
            note = notify.push_new_alerts(db, config or {}, on_alerts=on_alerts)
            return {'collect': summary, 'notify': note}
        finally:
            db.close()

    return refresh_once


def make_replier(db_path, root=None):
    """Build a reply closure with its own short-lived connection. Posts a
    decision comment to Gitea (comment_issue only) and records it in
    user_replies so the decision leaves the queue immediately. Returns the
    platform receipt; raises on write failure."""
    collect_root = root or collect.DEFAULT_ROOT

    def replier(org_repo, number, body):
        db = store.connect(db_path)
        try:
            row = db.execute('SELECT id, config_path FROM projects WHERE org_repo=?',
                             (org_repo,)).fetchone()
            if row is None:
                raise KeyError(org_repo)
            pid = row['id']
            cfg, _ = collect.load_config(row['config_path'])
            receipt = GitHub(cfg).write('comment_issue', {'number': number, 'body': body})
            store.record_user_reply(db, pid, number, body,
                                    receipt.get('created_at') or '')
            return receipt
        finally:
            db.close()

    return replier


def _project_service(db_path, root, coordinator, org_repo):
    """The coordinator-side Service for one project: the merged server's own
    authority (enqueue never needs a machine grant — grants gate dispatch and
    the worker claim, both still ahead of any execution). A project this
    server does not serve remotely still gets its ledger row: the command
    simply waits (and expires visibly) until an agent could ever serve it."""
    del db_path
    if coordinator is not None:
        try:
            return coordinator.service_for(org_repo)
        except ProtocolError as exc:
            if exc.code != 'PROJECT_NOT_REMOTE':
                raise ActionError(404 if exc.code == 'PROJECT_NOT_FOUND'
                                  else 502, exc.message or exc.code) from None
    collect_root = root or collect.DEFAULT_ROOT
    from ..cp.runtime import load_server_config
    path = Path(collect_root) / org_repo / 'control-plane.json'
    if not path.is_file():
        raise ActionError(404, 'unknown project')
    try:
        cfg = load_server_config(path)
    except ValueError as exc:
        raise ActionError(502, 'CONFIG_INVALID: %s' % exc) from None
    return Service(cfg, authorization={'host': 'cp-server', 'managed_by': 'registry'})


def make_command_trigger(db_path, root=None, coordinator=None, action='review_round'):
    """Build the unified click closure: the card click becomes a command on
    the project's ledger and is pushed to a live worker agent in the same
    call — no local fast path, no machine affinity (the merged server is just
    another dispatch source for the same fleet). The response already knows
    the destination (``machine``) when an agent was connected; with none, the
    command queues PENDING until one connects, and the TTL sweep expires a
    never-served wait loudly. Idempotent while a command is live: a double
    click returns the same command instead of stacking rounds. ``action``
    re-points the same contract at another command kind (repair_round); the
    worker refuses unknown actions, so only actions it executes may be wired.
    Returns {'ok': True, 'command': <id>, 'state': ..., 'machine': ...,
    'created': bool}; refuses with ActionError."""

    def trigger_review(org_repo, number=None, agent=None, model=None):
        if number is None or isinstance(number, bool) \
                or not isinstance(number, int) or number <= 0:
            raise ActionError(400, 'number must be a positive integer')
        try:
            agent, model = validated_spawn_options(agent, model)
        except ValueError as exc:
            raise ActionError(400, str(exc)) from None
        service = _project_service(db_path, root, coordinator, org_repo)
        try:
            row, created = service.enqueue_command(action, number,
                                                   agent=agent, model=model)
            if coordinator is not None:
                # Event trigger, in-process. Unconditional: a click that hits
                # the idempotent existing command is still exactly the moment
                # a freshly-connected agent should get it pushed.
                coordinator.dispatcher.dispatch(keys=[org_repo])
            latest = service.latest_command(action, number)
        finally:
            service.store.db.close()
        return {'ok': True, 'command': latest['id'], 'state': latest['state'],
                'machine': latest['claimed_by'], 'created': created}

    return trigger_review


def make_command_status(db_path, root=None, coordinator=None, action='review_round'):
    """Read-only view of a target's newest command — the click's answer to
    'where is my round now?' (queued / dispatched to <machine> / running /
    failed with the reason). Same project resolution as the trigger."""

    def status(org_repo, number):
        service = _project_service(db_path, root, coordinator, org_repo)
        try:
            latest = service.latest_command(action, number)
        finally:
            service.store.db.close()
        if latest is None:
            return {'command': None}
        return {'command': latest['id'], 'state': latest['state'],
                'machine': latest['claimed_by'], 'fail_reason': latest['fail_reason'],
                'attempts': latest['attempts'], 'created_at': latest['created_at']}

    return status


