"""Serve-side operator assembly (wired in scripts/cp_server.py cmd_serve).

Everything here honors the two hard rules from the spec: the evaluation point
is the fact-production point (event sink on Store.event, alert hook on
push_new_alerts, watchdog cursor on collect) and there is no scan thread —
the consumer drains what is pushed into its queue, and its only periodic
action is offering its OWN pending commands to the dispatcher (the ledger
floor), never re-deriving anyone else's state.
"""
import json
import queue
import re
import shutil
import threading
import time
from pathlib import Path

from ..cp.store import Store
from . import face, ingest as op_ingest, store as op_store

DRAIN_TIMEOUT_SECONDS = 30.0
# The same org__repo shape the coordinator's registry enforces (server.py) —
# re-checked here because the target string is session-controlled and is used
# to build `projects_root/key/state` (review MAJOR-3: no whitespace, no '/',
# no '..', no absolute path → no path traversal into arbitrary directories).
_PROJECT_KEY_RE = re.compile(r'[A-Za-z0-9_.\-]+__[A-Za-z0-9_.\-]+')


def make_event_sink(op_cfg, feed):
    """Store.event on_event callback: enqueue matching kinds only. Runs inside
    the writer's transaction — queue.put only, never I/O, never raising."""
    kinds = tuple(op_cfg['event_kinds'])

    def sink(kind, payload):
        if kind.startswith(face.SELF_PREFIX):
            return
        if kind in kinds or kind.startswith('exam.'):
            feed.put((kind, payload or {}, time.time()))

    return sink


def ingest_event_safely(op_store, op_cfg, feed, dispatcher, home=None):
    """Drain one queued event through the gate; fired → dispatch now. A gate
    failure is logged and dropped loudly — the sink must never break CP."""
    try:
        kind, payload, _ = feed.get_nowait()
    except queue.Empty:
        return False
    try:
        decision = op_ingest.ingest_event(op_store, op_cfg, kind, payload)
        if decision.get('decision') == 'fired':
            dispatcher.dispatch_operator()
    except Exception as exc:
        print('codeloop: operator ingest failed for %s: %s' % (kind, exc),
              flush=True)
    return True


def _github(args):
    """One gh CLI invocation (machine-local deploy; the CLI carries its
    own auth). Raises a domain code on any failure."""
    import subprocess
    exe = shutil.which('gh')
    if exe is None:
        raise ValueError('OPERATOR_GITHUB_MISSING: gh CLI not on PATH')
    try:
        done = subprocess.run([exe] + args, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError('OPERATOR_GITHUB_FAILED: %s' % exc) from None
    if done.returncode != 0:
        raise ValueError('OPERATOR_GITHUB_FAILED: %s' % (done.stderr or done.stdout))
    return {'ok': True, 'output': (done.stdout or '')[-400:]}


def _gitea(args):
    """One gitea CLI invocation (machine-local deploy; the CLI carries its
    own auth). Raises a domain code on any failure."""
    import subprocess
    exe = shutil.which('gitea')
    if exe is None:
        raise ValueError('OPERATOR_GITEA_MISSING: gitea CLI not on PATH')
    try:
        done = subprocess.run([exe] + args, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError('OPERATOR_GITEA_FAILED: %s' % exc) from None
    if done.returncode != 0:
        raise ValueError('OPERATOR_GITEA_FAILED: %s' % (done.stderr or done.stdout))
    return {'ok': True, 'output': (done.stdout or '')[-400:]}


def _wal_checkpoint(db_path):
    """PRAGMA wal_checkpoint(TRUNCATE) on the dashboard SQLite in place
    (issue #303). Low-risk maintenance: the serve process may keep its WAL
    connection open — SQLite serializes, reports busy, or truncates; we only
    report the outcome, never kill anything. A db that is not on WAL journal
    mode is a no-op (returned pages stay 0); busy means another writer is
    mid-transaction and the operator should retry later."""
    import sqlite3
    if not db_path or not Path(db_path).exists():
        raise ValueError('OPERATOR_TARGET_INVALID: wal_checkpoint needs a real '
                         'dashboard sqlite path (db=%r)' % db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        busy, log, checkpointed = conn.execute(
            'PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        return {'ok': True, 'busy': busy, 'log': log,
                'checkpointed': checkpointed}
    finally:
        conn.close()


def make_executor(projects_root, dispatcher, refresh_fn, dash_db=None):
    """Serve-side executors for the wired L1 verbs. collect_refresh reuses the
    same refresh closure the dashboard's 采集 button uses; command_resend
    enqueues on the project's own command ledger (idempotent while live) and
    nudges the dispatcher for that project only; wal_checkpoint TRUNCATEs the
    dashboard SQLite WAL in place (serve keeps running — checkpoint is
    cooperative and safe under concurrency); the gitea verbs write through
    the machine's gitea CLI. Target formats (see references/operator.md):
    create/escalate '<org__repo>:<title>' with detail=body; comment
    '<org__repo>:<issue#>' with detail=body; wal_checkpoint '<db path>' or
    '' to use the wired dash_db (passed from cp_server assembly)."""
    projects_root = Path(projects_root)

    def execute(verb, target, detail=''):
        if verb == 'collect_refresh':
            summary = refresh_fn()
            return {'ok': True, 'summary': summary}
        if verb == 'wal_checkpoint':
            return _wal_checkpoint(target or dash_db)
        if verb == 'command_resend':
            # target: "<review_round|repair_round>:<number>:<org__repo>"
            action, number, key = (target.split(':') + ['', ''])[:3]
            if action not in ('review_round', 'repair_round') or not number:
                raise ValueError('OPERATOR_TARGET_INVALID: %r' % target)
            if not _PROJECT_KEY_RE.fullmatch(key):
                # Review MAJOR-3: key builds `projects_root/key/state` — only
                # the registry's org__repo shape may reach the filesystem.
                raise ValueError('OPERATOR_TARGET_INVALID: %r (bad org__repo)'
                                 % target)
            state = projects_root / key / 'state'
            project = Store(state)
            try:
                row, created = project.enqueue_command(action, int(number))
                dispatched = dispatcher.dispatch(keys=[key]) if created else 0
                return {'ok': True, 'created': created, 'command': row['id'],
                        'dispatched': dispatched}
            finally:
                project.db.close()
        if verb in ('github_create_issue', 'github_comment_issue', 'gitea_create_issue', 'escalate_human', 'gitea_comment_issue'):
            is_github = verb.startswith('github_') or (verb == 'escalate_human' and shutil.which('gh') is not None)
            cli_name = 'gh' if is_github else 'gitea'
            if shutil.which(cli_name) is None:
                raise ValueError('OPERATOR_VERB_NOT_WIRED: %s (%s CLI not on PATH)' % (verb, cli_name))
            repo, _, rest = (target or '').partition(':')
            if not repo or not rest:
                raise ValueError('OPERATOR_TARGET_INVALID: %r (want <org__repo>:<title|#issue>)' % target)
            cli_repo = repo.replace('__', '/')
            if verb in ('github_comment_issue', 'gitea_comment_issue'):
                if is_github:
                    return _github(['issue', 'comment', rest.strip(), '--repo', cli_repo, '--body', detail or ''])
                return _gitea(['comment', rest.strip(), '--repo', cli_repo, '-b', detail or ''])
            title = rest.strip()
            if verb == 'escalate_human' and not title.startswith('[❓needs-human]'):
                title = '[❓needs-human] ' + title
            if is_github:
                return _github(['issue', 'create', '--repo', cli_repo, '--title', title, '--body', detail or title])
            return _gitea(['issue', 'create', '--repo', cli_repo, '-t', title, '-b', detail or title])
        raise ValueError('OPERATOR_VERB_NOT_WIRED: %s' % verb)

    return execute


def make_manual_trigger(op_store_conn, op_cfg, dispatcher, home=None):
    """POST /api/operator/trigger closure: a manual signal bypasses cooldown
    and cap, keeps single-live-round dedup, and dispatches in the same call.

    Signature is the shared trigger contract the serve POST block calls
    (project, number, agent, model) plus the optional manual note —
    must stay in lockstep with dash/serve.py trigger_paths, or every
    click 502s (review MAJOR)."""

    def trigger(project, number, agent, model, note=''):
        payload = {'project': project or '', 'number': number,
                   'agent': agent, 'model': model}
        if note:
            # manual-dialog note rides the signal payload; the ledger
            # serialization (serve /api/operator) renders signal.note.
            payload['note'] = note
        decision = op_ingest.check_and_record(
            op_store_conn, op_cfg, source='manual', kind='manual',
            key='manual|%d' % int(time.time() * 1000),
            payload=payload, org_repo=project or '', severity='mid',
            manual=True, home=home)
        if decision.get('decision') == 'fired' and dispatcher is not None:
            dispatcher.dispatch_operator()
        return {'ok': decision.get('decision') == 'fired',
                'decision': decision.get('decision'),
                'id': ('operator:%s' % decision['item']['id'])
                if decision.get('item') else None}

    return trigger


def make_options(op_cfg, dispatcher):
    """GET /api/operator/options: machine defaults + serve-host spawn options.
    serve-host is the honest scope for operator rounds (they always run here).
    The response carries the shared ``ok`` envelope the front-end's
    openTriggerDialog contract requires (issue #301 defect 1: without it the
    dialog's ``if (!resp.ok || !data.ok)`` always threw and the agent list
    never rendered)."""
    from ..agent_runners import cards

    def options():
        return {'ok': True,
                'enabled': bool(op_cfg and op_cfg['enabled']),
                'default_agent': (op_cfg or {}).get('agent'),
                'default_model': (op_cfg or {}).get('model'),
                'agents': cards(),
                'scope': {'kind': 'serve-host'}}

    return options


def start_consumer(op_store_conn, op_cfg, dispatcher, watchdog_log, dash_db=None,
                   home=None, floor=300.0):
    """The operator consumer: event-driven drain, plus the ledger floor —
    a periodic offer of the operator store's own pending commands and a
    watchdog-cursor ingest. No state is re-derived anywhere."""
    feed = queue.Queue()
    coordinator_sink = make_event_sink(op_cfg, feed)

    def loop():
        while True:
            served = True
            while served:
                served = ingest_event_safely(op_store_conn, op_cfg, feed,
                                             dispatcher, home=home)
            try:
                # issue #302 defect 3 (review): the operator store is fed by
                # the raw CommandStore (dispatch_operator lists PENDING without
                # a sweep), so a command stuck CLAIMED (worker died mid-
                # dispatch, never acked SPAWNED/FAILED) stays CLAIMED forever
                # and the drain below sees it as "live". Sweep first to requeue
                # stale claims (→PENDING, dispatchable again) or FAILED them
                # (→ drainable), then offer commands.
                op_store_conn.sweep_commands()
            except Exception as exc:
                print('codeloop: operator command sweep failed: %s' % exc, flush=True)
            try:
                # 2026-10-15 复盘: RUNNING items whose executions are all
                # terminal (pre-#302 PROCESS_DIED vintage) wedge the signal
                # key forever — re-fires die on UNIQUE(leases.resource). The
                # floor is the only path that heals the leftover.
                op_store.recycle_wedged_rounds(op_store_conn)
            except Exception as exc:
                print('codeloop: operator wedged-round recycle failed: %s' % exc,
                      flush=True)
            try:
                dispatcher.dispatch_operator()
            except Exception as exc:
                print('codeloop: operator dispatch failed: %s' % exc, flush=True)
            try:
                # issue #302 defect 3: the ledger floor also reoffers READY
                # operator items whose command was never live (dispatch fail /
                # cap truncation) — otherwise a stuck READY mutely blocks the
                # same signal key (active_signal → skipped_active).
                op_store.drain_obsolete_readys(op_store_conn, floor)
            except Exception as exc:
                print('codeloop: operator drain failed: %s' % exc, flush=True)
            try:
                op_ingest.ingest_watchdog(op_store_conn, op_cfg, watchdog_log,
                                          db=dash_db, home=home)
            except Exception as exc:
                print('codeloop: operator watchdog ingest failed: %s' % exc,
                      flush=True)
            try:
                feed.get(timeout=DRAIN_TIMEOUT_SECONDS)   # next event, or floor tick
            except queue.Empty:
                pass

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return coordinator_sink, feed
