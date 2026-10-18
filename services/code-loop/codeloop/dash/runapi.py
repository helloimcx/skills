"""Run-detail payload assembly for GET /api/run (split out of serve.py to
keep it under the 800-line red line).

Three sources, one payload:
- execution facts: the project's live control-plane store first, the
  collected mirror as fallback (same arbiter order as the board);
- session: the round's stream, in order of specificity — the bound
  transcript through the spawning agent's plugin, the round's own CLI
  log, the agent-reported stream — read incrementally from a byte
  offset (or a seq for reported rounds);
- timeline/evidence: the deterministic control-plane events — always
  derivable, so an unbound session degrades the view instead of blanking it.

The endpoint is read-only: no spawn-path change, no CP write, no platform
write."""
import json
import re
import sqlite3
import time
from pathlib import Path

from .. import agent_runners, trigger

EXEC_ID_RE = re.compile(r'^[0-9a-f]{32}$')
# The other /api/run exec shape: a derived subagent archive id
# ('<platform>:<session>', e.g. 'zcode:sess_…' / 'claude:<stem>#<tool_use_id>').
AGENT_RUN_ID_RE = re.compile(r'^[a-z][a-z0-9_-]{0,31}:\S{1,300}$')


def _state_dir(project_row):
    """The project's machine-local CP state dir (where the round's own log
    lives beside the store)."""
    return Path(project_row['config_path'] or '').parent / 'state'


def _cp_state_db(config_path):
    path = Path(config_path or '').parent / 'state' / 'control-plane.sqlite'
    return path if path.is_file() else None


def _cp_rows(cp_db, query, params=()):
    uri = 'file:%s?mode=ro' % Path(cp_db).as_posix()
    source = sqlite3.connect(uri, uri=True)
    source.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in source.execute(query, params)]
    finally:
        source.close()


def _jsonish(raw):
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _run_item(wi_key, wi_payload):
    item = {'key': wi_key, 'number': None, 'title': ''}
    try:
        pr = json.loads(wi_payload or '{}').get('pr') or {}
    except ValueError:
        pr = {}
    number = pr.get('number')
    if isinstance(number, int) and not isinstance(number, bool):
        item['number'] = number
    item['title'] = pr.get('title') or ''
    return item


def _live_execution(cp_db, exec_id):
    """(execution_row, item, timeline, evidence) from the live store; item is
    None when the execution is unknown there."""
    rows = _cp_rows(
        cp_db,
        'SELECT ex.*, wi.key AS wi_key, wi.payload AS wi_payload '
        'FROM executions ex LEFT JOIN work_items wi ON wi.id=ex.item_id '
        'WHERE ex.id=?', (exec_id,))
    if not rows:
        return None, None, [], []
    row = rows[0]
    item = _run_item(row.pop('wi_key', None), row.pop('wi_payload', None))
    timeline = [{'kind': r['kind'], 'at': r['created_at'],
                 'payload': _jsonish(r['payload'])}
                for r in _cp_rows(cp_db,
                                  'SELECT kind, payload, created_at FROM events '
                                  'WHERE payload LIKE ? ORDER BY id',
                                  ('%' + exec_id + '%',))]
    evidence = [{'kind': r['kind'], 'at': r['created_at'],
                 'payload': _jsonish(r['payload'])}
                for r in _cp_rows(cp_db,
                                  'SELECT kind, payload, created_at FROM evidence '
                                  'WHERE execution_id=? ORDER BY created_at',
                                  (exec_id,))]
    return row, item, timeline, evidence


def _mirror_execution(db, pid, exec_id):
    """(execution, timeline) from the collected runtime mirror; the mirror has
    no evidence table, so evidence stays empty on this path."""
    rows = db.execute(
        'SELECT ex.worker, ex.state, ex.result, ex.worktree, ex.branch, ex.head,'
        ' ex.expires_at, ex.created_at, wi.key AS wi_key, wi.payload AS wi_payload '
        'FROM executions ex LEFT JOIN work_items wi '
        'ON wi.project_id=ex.project_id AND wi.cp_id=ex.item_cp_id '
        'WHERE ex.project_id=? AND ex.cp_id=?', (pid, exec_id)).fetchall()
    if not rows:
        return None, [], []
    row = dict(rows[0])
    wi_key = row.pop('wi_key', None)
    wi_payload = row.pop('wi_payload', None)
    execution = {'id': exec_id, **row}
    timeline = [{'kind': r['kind'], 'at': r['created_at'],
                 'payload': _jsonish(r['payload'])}
                for r in db.execute(
                    'SELECT kind, payload, created_at FROM cp_events '
                    'WHERE project_id=? AND payload LIKE ? ORDER BY cp_id',
                    (pid, '%' + exec_id + '%'))]
    return (execution, wi_key, wi_payload), timeline, []


def _progress_session(cp_db, exec_id, since):
    """The agent-reported stream for a round whose transcript lives on the
    agent machine (remote execution): same flat events the local tail
    produces, read incrementally by seq instead of byte offset. ``None``
    when the store has no stream for this execution (or no table — a
    legacy store degrades to the unbound placeholder, never a 500)."""
    try:
        rows = _cp_rows(cp_db,
                        'SELECT seq, payload, created_at FROM progress_events '
                        'WHERE execution=? AND seq>? ORDER BY seq',
                        (exec_id, since))
    except sqlite3.Error:
        return None
    events = []
    for row in rows:
        try:
            payload = json.loads(row['payload'])
        except (ValueError, TypeError):
            continue
        events.append(dict(payload, at=row['created_at']))
    if not rows:
        # "No stream" and "stream exhausted" are different states (issue
        # #215): the former is the unbound placeholder (timeline only), the
        # latter is a reported session with nothing new. Distinguish by
        # asking whether this execution has any stream row at all.
        try:
            last = _cp_rows(cp_db,
                            'SELECT MAX(seq) AS last FROM progress_events '
                            'WHERE execution=?', (exec_id,))
        except sqlite3.Error:
            return None
        if not last or last[0]['last'] is None:
            return None   # no stream for this execution: the unbound placeholder
        cursor = last[0]['last']
        return {'bound': 'reported', 'path': None, 'events': [],
                'offset': cursor, 'size': cursor, 'skipped': 0}
    cursor = rows[-1]['seq']
    return {'bound': 'reported', 'path': None, 'events': events,
            'offset': cursor, 'size': cursor, 'skipped': 0}


def _execution_agent(cp_db, exec_id):
    """The agent an execution was spawned with, from the command ledger row
    (the click that enqueued it named it). ``None`` — no row, or an
    unreadable store — means the default registered-command path, which the
    reader dispatch maps to claude exactly as every round before pluggable
    agents read."""
    if cp_db is None:
        return None
    try:
        rows = _cp_rows(cp_db,
                        'SELECT agent FROM commands WHERE execution=? '
                        'ORDER BY id DESC LIMIT 1', (exec_id,))
    except sqlite3.Error:
        return None
    return rows[0]['agent'] if rows else None


def _round_log_session(project_row, exec_id, since):
    """The round's own CLI output (``<state>/executions/<exec_id>.log``), the
    stream for a round whose session lives in another CLI's storage — an
    opencode round keeps no claude transcript to bind. Same flat events, byte
    offset as the cursor. A log that yields nothing this reader can show (empty,
    or only lines it cannot parse) is NO information, not a binding: the caller
    falls through rather than showing a bound-but-blank session."""
    path = trigger.spawn_log_path(_state_dir(project_row), exec_id)
    if path is None:
        return None
    try:
        if path.stat().st_size == 0:
            return None
    except OSError:
        return None
    tail = agent_runners.claude_transcript.tail(path, offset=since)
    if not tail['events'] and not since:
        return None   # nothing on the first read: the timeline is the honest view
    return {'bound': 'spawn-log', 'path': str(path), **tail}


def _session_events(project_row, exec_id, execution, since, cp_db=None):
    """Resolve and incrementally read the round's stream, in order of
    specificity: a content-exact transcript bound through the spawning
    agent's plugin (richest, and certain — the prompt embeds the execution
    id), the round's own CLI log, a *guessed* single-candidate transcript
    (matched by time window only, so it ranks below the log that is keyed by
    execution id), the agent-reported stream (remote rounds, #210), and
    finally the unbound placeholder so the view falls back to the
    deterministic timeline instead of erroring."""
    session = {'bound': None, 'path': None, 'events': [], 'offset': since,
               'size': 0, 'skipped': 0}
    repo_cwd = project_row['repo_cwd']
    try:
        created_at = float(execution.get('created_at'))
    except (TypeError, ValueError):
        created_at = None
    guess = None
    if repo_cwd and created_at is not None:
        runner = agent_runners.session_reader(_execution_agent(cp_db, exec_id))
        bound = runner.resolve_session(repo_cwd, exec_id, created_at)
        if bound['bound'] == 'exact':
            session.update(bound)
            session.update(runner.tail(bound['path'], offset=since))
            return session
        guess = bound if bound['path'] else None
    local = _round_log_session(project_row, exec_id, since)
    if local is not None:
        return local
    if guess is not None:
        session.update(guess)
        session.update(runner.tail(guess['path'], offset=since))
        return session
    if cp_db is not None:
        reported = _progress_session(cp_db, exec_id, since)
        if reported is not None:
            return reported
    return session


def run_detail(db, project_row, exec_id, since):
    """The run-detail payload, or None when neither store knows the
    execution (the HTTP layer maps that to 404). An agent-run id (the
    ``platform:session`` form) routes to the derived subagent archive
    instead — same payload shape, stream bound as ``agent-session``
    (see historyapi); the project row is irrelevant there, the archive
    carries its own cwd. An ``operator:<32hex>`` id routes to the
    machine-level operator store (live read, same payload shape)."""
    if exec_id.startswith('operator:'):
        return _operator_detail(exec_id, since)
    if not EXEC_ID_RE.match(exec_id.lower()):
        from . import historyapi
        return historyapi.agent_run_detail(db, exec_id, since)
    execution, item = None, {'key': None, 'number': None, 'title': ''}
    timeline, evidence = [], []
    cp_db = _cp_state_db(project_row['config_path'])
    if cp_db is not None:
        try:
            execution, item, timeline, evidence = _live_execution(cp_db, exec_id)
        except sqlite3.Error:
            pass    # live store unreadable: the mirror fallback takes over
    if execution is None:
        found, timeline, evidence = _mirror_execution(db, project_row['id'], exec_id)
        if found is not None:
            execution, wi_key, wi_payload = found
            item = _run_item(wi_key, wi_payload)
    if execution is None:
        return None
    return {'execution': execution, 'item': item,
            'session': _session_events(project_row, exec_id, execution, since,
                                       cp_db=cp_db),
            'timeline': timeline, 'evidence': evidence}


def _operator_state_dir():
    """Indirection so tests can point the operator deep link at a temp home."""
    from ..operator import store as op_store
    return op_store.state_dir()


def _operator_detail(exec_id, since):
    """view=run for an operator round: live read of the machine-level
    operator store (read-only connection), payload identical to a CP round.
    Session is always the round's own log (spawn-log binding by construction)."""
    state = _operator_state_dir()
    db_path = state / 'control-plane.sqlite'
    if not db_path.is_file():
        return None
    real = exec_id[len('operator:'):]
    source = sqlite3.connect('file:%s?mode=ro' % db_path.as_posix(), uri=True)
    source.row_factory = sqlite3.Row
    try:
        # MAJOR-3: the board's ledger + manual triggers carry the *signal*
        # (work_item) id, the front-end deep link carries it too — look the
        # execution up by either id, falling back to the newest execution of
        # the signal when the id is the item's (a signal supersedes: each
        # put_signal mints a fresh item id, so "latest" is well-defined).
        row = source.execute(
            'SELECT * FROM executions WHERE id=? OR item_id=? '
            'ORDER BY created_at, id LIMIT 1', (real, real)).fetchone()
        if row is None:
            return None
        row = source.execute(
            'SELECT * FROM executions WHERE id=? OR item_id=? '
            'ORDER BY created_at, id LIMIT 1', (real, real)).fetchone()
        if row is None:
            return None
        execution = dict(row)
        # issue #301 defect 2: the deep link may carry the *signal* (work_item)
        # id — the lookup above resolved it to the real execution, so every
        # downstream read (log file, evidence, timeline) must use the
        # execution id. The old code kept the raw URL id, so a signal deep
        # link found the row but then bound to a nonexistent
        # executions/<item_id>.log and matched only item-id events.
        real = execution['id']
        item_row = source.execute('SELECT key,payload FROM work_items WHERE id=?',
                                  (execution['item_id'],)).fetchone()
        payload = json.loads(item_row['payload']) if item_row is not None else {}
        item = {'key': item_row['key'] if item_row else None, 'number': None,
                'title': payload.get('message') or payload.get('note') or ''}
        # Timeline events carry either the execution id (execution.* kinds) or
        # the item id (item.created / item_recycled) — match both so a signal
        # deep link renders the full session, not just item-id events.
        marker_exec = '%' + execution['id'] + '%'
        marker_item = '%' + execution['item_id'] + '%'
        timeline = [dict(r) for r in source.execute(
            'SELECT kind,payload,created_at AS at FROM events '
            'WHERE payload LIKE ? OR payload LIKE ? '
            'ORDER BY created_at DESC LIMIT 500',
            (marker_exec, marker_item))][::-1]
        evidence = [dict(r) for r in source.execute(
            'SELECT kind,payload,created_at AS at FROM evidence '
            'WHERE execution_id=? ORDER BY created_at', (real,))]
        log = state / 'executions' / ('%s.log' % real)
        session = {'bound': 'spawn-log' if log.is_file() else None,
                   'path': str(log) if log.is_file() else None,
                   'events': [], 'offset': since, 'size': 0, 'skipped': 0}
        if log.is_file():
            data = log.read_bytes()
            session['size'] = len(data)
            offset = min(max(int(since or 0), 0), len(data))
            fresh = data[offset:]
            session['offset'] = len(data)
            session['events'] = [{'t': time.time(), 'text': line}
                                 for line in fresh.decode('utf-8',
                                                          errors='replace').splitlines()]
        return {'execution': execution, 'item': item, 'session': session,
                'timeline': timeline, 'evidence': evidence}
    finally:
        source.close()
