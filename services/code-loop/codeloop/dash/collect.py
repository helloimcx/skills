"""Collector: gather platform + control-plane facts into the global store.

Per project it reuses the proven cp.gitea snapshot (pagination, TLS pinning,
credential fallback) and diff-applies it at the storage layer; control-plane
runtime tables are small and mutable, so they are replaced wholesale from a
read-only copy of the project's SQLite. One project failing must not stop the
others; the summary reports failures and the CLI maps them to a non-zero exit.
"""
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import store
from .agentruns import sweep as sweep_agent_runs
from ..cp import observer
from ..cp.github import GitHub, GitHubError
Gitea = GitHub
GiteaError = GitHubError

DEFAULT_DB = Path.home() / '.code-loop' / 'dashboard.sqlite'
DEFAULT_ROOT = Path.home() / '.code-loop' / 'projects'

# How old the shared observer store may be before the collector crawls itself.
# The CP server's driver keeps it far fresher; this only matters when the
# server is down, and keeps the dashboard working standalone.
OBSERVER_TTL = 600

# control-plane table name -> runtime key in store.replace_runtime
CP_TABLES = {'work_items': 'work_items', 'executions': 'executions',
             'leases': 'leases', 'outbox': 'outbox', 'events': 'cp_events',
             'commands': 'commands'}


class CollectError(RuntimeError):
    pass


def discover_projects(root):
    if not Path(root).is_dir():
        return []
    return sorted(Path(root).glob('*/control-plane.json'))


def load_config(config_path):
    config_path = Path(config_path)
    try:
        raw = json.loads(config_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise CollectError('unreadable config %s: %s' % (config_path, exc)) from exc
    platform = raw.get('platform')
    if not isinstance(platform, dict) or not platform.get('url') or not platform.get('repo'):
        raise CollectError('config %s lacks platform.url/repo' % config_path)
    cfg = {'url': platform['url'], 'repo': platform['repo'],
           'token_env': platform.get('token_env', 'GITHUB_TOKEN'),
           'base': raw.get('base', 'main'),
           'timeout': platform.get('timeout', 30),
           'page_limit': platform.get('page_limit', 50),
           'max_pages': platform.get('max_pages', 1000),
           # The CP injects these into the same adapter config; both crawls
           # feed one store, so both must fetch the same facts.
           'required_checks': raw.get('required_checks', [])}
    if platform.get('ca_file'):
        ca = Path(platform['ca_file'])
        if not ca.is_absolute():
            ca = config_path.parent / ca
        cfg['ca_file'] = str(ca)
    return cfg, raw.get('base', '')


def read_enabled(project_dir):
    marker = Path(project_dir) / 'state' / 'machine-enabled.json'
    if not marker.is_file():
        return 0, '', None
    try:
        data = json.loads(marker.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return 0, '', None
    enabled_at = data.get('enabled_at')
    return (1, str(data.get('host', '')),
            float(enabled_at) if isinstance(enabled_at, (int, float)) else None)


def copy_runtime(db, pid, cp_db_path):
    """Read one consistent source snapshot, then atomically project it."""
    uri = Path(cp_db_path).expanduser().resolve().as_uri() + '?mode=ro'
    source = sqlite3.connect(uri, uri=True)
    source.row_factory = sqlite3.Row
    try:
        source.execute('BEGIN')
        present = {row[0] for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        runtime = {}
        for table, key in CP_TABLES.items():
            runtime[key] = ([dict(row) for row in source.execute('SELECT * FROM ' + table)]
                            if table in present else [])
        source.commit()
    finally:
        source.close()
    store.replace_runtime(db, pid, runtime)
    return {key: len(rows) for key, rows in runtime.items()}


def collect_project(db_path, config_path, now=None):
    """Collect one project; raises CollectError after recording the failure.

    Owns its own store connection (PR #180 review MAJOR-1): a shared sqlite3
    connection across threads interleaves transactions — one worker's
    rollback silently discards another's uncommitted writes. Each worker
    connects to the same db_path independently; sqlite3's lock + busy_timeout
    serialize writers (PR #134 semantics preserved — a busy write becomes
    this project's CollectError, never a crashed run). An unguarded write
    used to kill the whole run with zero trace, silently starving every
    project discovered after this one (e.g. downstream projects
    for ~10h while the alphabetically first project kept collecting)."""
    config_path = Path(config_path)
    started = time.time()
    moment = now if now is not None else started
    org_repo = config_path.parent.name
    db = store.connect(db_path)
    try:

        def record_failure(message):
            try:
                failure_pid = store.upsert_project(db, org_repo, last_error=message)
                store.log_sync(db, failure_pid, ok=False, stats={}, started=started,
                               finished=time.time(), error=message)
            except sqlite3.Error:
                pass  # the store is still busy; the run summary carries the failure

        try:
            platform_cfg, base = load_config(config_path)
        except CollectError as exc:
            record_failure('%s: %s' % (org_repo, exc))
            raise
        try:
            pid = store.upsert_project(db, org_repo, str(config_path),
                                       platform_url=platform_cfg['url'],
                                       repo=platform_cfg['repo'], base=base,
                                       last_collect_at=moment, last_error=None)
            enabled, host, enabled_at = read_enabled(config_path.parent)
            store.upsert_project(db, org_repo, enabled=enabled, enabled_host=host,
                                 enabled_at=enabled_at)
            # Facts come from the shared observer store (the CP server's driver
            # keeps it fresh; the CP reads the very same rows). Serving this
            # collector's own crawl on a miss keeps the dashboard working when
            # the server is down — and refreshes the store for everyone.
            # The dashboard passes its own known items as the incremental basis
            # (issue #165): a closed+unchanged PR the dashboard has never seen
            # must be fully crawled (comments/reviews), not skipped by the
            # observer's own basis and stored with empty threads.
            counts = store.apply_snapshot(
                db, pid, observer.snapshot(observer.default_db_path(), org_repo,
                                           OBSERVER_TTL, GitHub(platform_cfg).snapshot,
                                           known=store.known_items(db, pid)))
            cp_db = config_path.parent / 'state' / 'control-plane.sqlite'
            runtime = copy_runtime(db, pid, cp_db) if cp_db.is_file() else {}
            store.upsert_project(db, org_repo, last_ok_collect_at=moment)
            stats = dict(counts, **runtime)
            store.log_sync(db, pid, ok=True, stats=stats, started=started, finished=time.time())
            return {'ok': True, 'stats': stats}
        # ValueError: the observer store rejects a malformed crawl
        # (INVALID_SNAPSHOT) — one project's bad facts must not stop the rest.
        except (GitHubError, CollectError, sqlite3.Error, OSError, ValueError) as exc:
            message = '%s: %s' % (org_repo, exc)
            record_failure(message)
            raise CollectError(message) from exc
    finally:
        db.close()


DEFAULT_REGISTRY = Path.home() / '.code-loop' / 'server' / 'registry.sqlite'


def collect_machines(db, registry_path=DEFAULT_REGISTRY):
    """Snapshot the server machine registry (read-only) when one exists."""
    registry_path = Path(registry_path)
    if not registry_path.is_file():
        return None
    uri = 'file:%s?mode=ro' % registry_path.as_posix()
    source = sqlite3.connect(uri, uri=True)
    source.row_factory = sqlite3.Row
    try:
        present = {row[0] for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if 'machines' not in present:
            return None
        machines = []
        for row in source.execute('''SELECT m.*, (SELECT group_concat(org_repo, ',')
                FROM grants g WHERE g.machine_id=m.id) AS projects FROM machines m'''):
            machines.append(dict(row))
        store.replace_machines(db, machines)
        return machines
    finally:
        source.close()


def collect(db, root=DEFAULT_ROOT, only=None, registry_path=None,
            max_workers=5, db_path=None):
    """Collect every discovered project into an open store; failures isolated.

    No process lock: overlapping schedules (a 2-minute task while a manual
    POST /api/collect runs) are safe by construction, because concurrent
    writers serialize on sqlite itself and every write is an idempotent
    upsert. A filesystem lock was the original guard (issues #187/#205), but
    it had no recovery from a leaked lock held by a live process — the holder
    stayed alive forever and the stale-timeout takeover never fired, silently
    stopping collection (2026-09-23). The lock is gone; the guarantees it
    stood for live in the schema and the write path instead:
      * UNIQUE(project_id, number) on issues/prs and UNIQUE(project_id,
        cp_id) on the runtime mirrors make a racing duplicate insert a
        no-op upsert, never a duplicate row.
      * busy_timeout=15 + WAL (PR #134 semantics preserved — a busy write
        becomes that project's CollectError, never a crashed run).
      * _upsert_fact is an atomic INSERT ... ON CONFLICT, so two collectors
        racing the same fresh row both succeed (one inserts, one updates).

    Projects are collected concurrently (issue #160, user decision 1A 2B):
    the per-project crawl is network-bound (Gitea snapshots), so parallel
    workers cut the wall-clock of a full run. Each worker owns an independent
    store connection (PR #180 review MAJOR-1): a shared sqlite3 connection
    interleaves transactions across threads, so one worker's rollback would
    silently discard another's uncommitted writes. max_workers defaults to 5
    (decision 2B). `db` stays the caller's connection for the single-threaded
    machine-registry snapshot."""
    configs = discover_projects(root)
    if only is not None:
        wanted = set(only)
        configs = [c for c in configs if c.parent.name in wanted]
    if db_path is None:
        # Legacy callers pass only a connection; derive the path from it
        # so workers can open their own (sqlite3 exposes no public path).
        db_path = db.execute('PRAGMA database_list').fetchone()[2]
    failed = _collect_projects(db_path, configs, max_workers)
    try:
        collect_machines(db, registry_path or DEFAULT_REGISTRY)
    except (sqlite3.Error, OSError) as exc:
        failed.append('machine registry: %s' % exc)
    # Agent-run archives: machine-local, project-independent, deterministic
    # (spec docs/specs/2026-09-21-dash-execution-history.md). One source
    # failing is recorded and never stops the platform collection above.
    try:
        sweep_agent_runs(db)
    except (sqlite3.Error, OSError, ValueError) as exc:
        failed.append('agent-runs: %s' % exc)
    # Operator mirror: machine-level, read-only projection of the operator
    # store (spec docs/specs/2026-09-30-cp-operator-agent.md). Absent store
    # (operator disabled everywhere) is a clean no-op.
    try:
        collect_operator(db)
    except (sqlite3.Error, OSError, ValueError) as exc:
        failed.append('operator: %s' % exc)
    return {'projects': len(configs), 'failed': failed}


def collect_operator(db, home=None, window_days=30):
    """Mirror the operator store's signals and rounds into the dash store.

    Pure projection with a bounded window: only rows younger than
    ``window_days`` are read from the machine-global operator store
    (read-only connection; absent file leaves the tables empty), and the
    mirror upserts by primary key while pruning rows older than the window —
    an O(live window) merge instead of a full DELETE+INSERT rebuild that grew
    with the append-only operator history (review MINOR). The operator store
    stays the single source of truth — the mirror exists so dashboard queries
    never open the live store."""
    from ..operator import store as op_store
    import json as _json
    state = op_store.state_dir(home)
    if not state.is_dir():
        return
    window = time.time() - window_days * 86400
    source = sqlite3.connect(
        'file:%s?mode=ro' % (state / 'control-plane.sqlite').as_posix(), uri=True)
    source.row_factory = sqlite3.Row
    try:
        signals = [dict(r) for r in source.execute(
            "SELECT id,key,payload,state,created_at FROM work_items "
            "WHERE kind='operator' AND created_at>=?", (window,))]
        runs = [dict(r) for r in source.execute(
            '''SELECT e.id,e.item_id,e.worker,e.state,e.result,e.created_at,
                      c.agent,c.model
               FROM executions e
               JOIN work_items i ON i.id=e.item_id AND i.kind='operator'
               LEFT JOIN commands c ON c.execution=e.id
               WHERE e.created_at>=?''', (window,))]
    finally:
        source.close()
    with db:
        db.execute('DELETE FROM operator_signals WHERE created_at<?', (window,))
        db.execute('DELETE FROM operator_runs WHERE started_at<?', (window,))
        for s in signals:
            payload = _json.loads(s['payload'])
            _, rest = s['key'].split(':', 1)
            sig_source, sig_key = rest.split(':', 1)
            db.execute('''INSERT INTO operator_signals (id,source,kind,key,org_repo,
                severity,message,payload,state,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET state=excluded.state,
                    payload=excluded.payload, severity=excluded.severity,
                    message=excluded.message''',
                (s['id'], sig_source, payload.get('kind', ''), sig_key,
                 payload.get('org_repo', ''), payload.get('severity'),
                 payload.get('message'), s['payload'], s['state'], s['created_at']))
        for r in runs:
            db.execute('''INSERT INTO operator_runs (id,signal_id,worker,state,result,
                agent,model,started_at,ended_at) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET state=excluded.state,
                    result=excluded.result, agent=excluded.agent,
                    model=excluded.model''',
                (r['id'], r['item_id'], r['worker'], r['state'], r['result'],
                 r.get('agent'), r.get('model'), r['created_at'], None))


def _collect_projects(db_path, configs, max_workers):
    """Run the per-project crawls concurrently; each worker owns an
    independent store connection (PR #180 MAJOR-1)."""
    failed = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(collect_project, db_path, config_path)
                   for config_path in configs}
        for future in futures:
            try:
                future.result()
            except CollectError as exc:
                failed.append(str(exc))
    return failed
