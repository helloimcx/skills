"""The opencode plugin's session-storage reader (the output face).

``opencode run`` persists a session as JSON files under
``~/.local/share/opencode/storage/``: ``session/<project>/<ses>.json``
(carries ``directory`` and wall times), ``message/<ses>/<msg>.json``
(role + millisecond times) and ``part/<msg>/<prt>.json`` — typed parts:
``text`` (the verbatim prompt *and* answers, which is what makes binding
content-exact: the review prompt embeds ``execution=<uuid>`` in its first
line), ``reasoning`` (thinking), ``tool`` (state.status/input/output) and
lifecycle markers like ``step-start``. Only these files are read —
``opencode.db`` is the CLI's internal WAL database and is never touched.

Same two tiers as the claude reader: ``exact`` wins by content (newest
first, bounded scan), ``window`` only when exactly one recent session
matches the repo directory; anything else stays unbound. Parsing is
fail-soft: a malformed file is skipped (counted in ``skipped``), never
raised. ``tail()`` is storage-backed, so its resume cursor is an event
count rather than a byte offset — opaque to every consumer — and ``size``
is the session's total event count so the client's exhaustion test
(``offset >= size``) holds in the same units; ``bytes`` carries the
part-file byte volume for the client's readout.
"""
import json
import os
from pathlib import Path

from .base import (BIND_SLOP_BEFORE, BIND_WINDOW_AFTER, EVENT_LIMIT,
                   cap_text, session_event, unbound, valid_exec_id)

STORAGE_ROOT = Path.home() / '.local' / 'share' / 'opencode' / 'storage'
SCAN_SESSIONS_MAX = 50   # binding reads at most this many newest sessions
OUT_CAP = 4000           # tool output attached to an event


def _load(path):
    try:
        return json.loads(path.read_text(encoding='utf-8', errors='replace'))
    except (OSError, ValueError):
        return None


def _ms(value):
    """Millisecond epoch → seconds, or None when absent/malformed."""
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None


def _norm_dir(value):
    return os.path.normcase(os.path.normpath(str(value or '')))


def _json_files(directory):
    try:
        return sorted(Path(directory).glob('*.json'))
    except OSError:
        return []


def _message_event_time(message, part):
    """A part's timestamp: its own start when it has one, else the message's
    creation — same epoch-seconds shape the claude reader emits."""
    return (_ms((part.get('time') or {}).get('start'))
            or _ms((message.get('time') or {}).get('created')))


def _part_event(message, part):
    """One typed part → a flat event, or None for lifecycle markers and
    anything unreadable — a foreign CLI's storage contract, never trusted."""
    if not isinstance(message, dict) or not isinstance(part, dict):
        return None
    kind = part.get('type')
    t = _message_event_time(message, part)
    if kind == 'text':
        text = str(part.get('text') or '').strip()
        return session_event(t, text) if text else None
    if kind == 'reasoning':
        text = str(part.get('text') or '').strip()
        return session_event(t, text, fold=True) if text else None
    if kind == 'tool':
        return _tool_event(part, t)
    return None


def _tool_event(part, t):
    """The tool part's command-like mono line plus its attached output;
    state shapes vary by CLI version and are never trusted."""
    state = part.get('state') if isinstance(part.get('state'), dict) else {}
    tool_input = state.get('input') if isinstance(state.get('input'), dict) else {}
    command = tool_input.get('command')
    if isinstance(command, str) and command:
        text = '$ ' + command
    else:
        text = str(state.get('title') or part.get('tool') or '')
    out = state.get('output')
    if not isinstance(out, str):
        meta = state.get('metadata') if isinstance(state.get('metadata'), dict) else {}
        out = meta.get('output')
    status = 'err' if state.get('status') == 'error' else None
    return session_event(t, text, mono=True,
                         out=cap_text(out, OUT_CAP), status=status)


def _session_parts(message_id):
    """The parts of one message, in id order — opencode ids are time-derived,
    so id order is the write order."""
    parts = []
    for path in _json_files(Path(STORAGE_ROOT) / 'part' / str(message_id or '')):
        data = _load(path)
        if isinstance(data, dict):
            parts.append(data)
    parts.sort(key=lambda part: str(part.get('id') or ''))
    return parts


def _session_messages(session_id):
    messages = []
    for path in _json_files(Path(STORAGE_ROOT) / 'message' / str(session_id or '')):
        data = _load(path)
        if isinstance(data, dict) and data.get('id'):
            messages.append(data)
    messages.sort(key=lambda m: (_ms((m.get('time') or {}).get('created')) or 0.0,
                                 str(m.get('id') or '')))
    return messages


def _prompt_has_exec(session_id, exec_id):
    """The exact tier's content check: the session's first user message is
    the spawned prompt; its text parts embed the execution id."""
    for message in _session_messages(session_id):
        if message.get('role') != 'user':
            continue
        for part in _session_parts(message.get('id')):
            if part.get('type') != 'text':
                continue
            if exec_id in str(part.get('text') or '').lower():
                return True
        return False   # first user message read: it is the prompt, decide here
    return False


def _recent_sessions(created_at):
    """Newest-first recent sessions as ``(mtime, data)`` pairs, bounded —
    the scan is linear in a machine's session count, so it must stay capped."""
    pairs = []
    try:
        paths = [p for p in (Path(STORAGE_ROOT) / 'session').glob('*/*.json')
                 if p.is_file()]
    except OSError:
        return pairs
    for path in paths:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= created_at - BIND_SLOP_BEFORE:
            data = _load(path)
            if isinstance(data, dict) and data.get('id'):
                pairs.append((mtime, data))
    pairs.sort(key=lambda pair: pair[0], reverse=True)
    return pairs[:SCAN_SESSIONS_MAX]


def resolve_session(repo_cwd, exec_id, created_at):
    """Bind an execution to an opencode session. Returns
    ``{'bound': 'exact'|'window'|None, 'path': session_id|None}`` — the
    handle is the session id, which ``tail`` re-reads storage from."""
    exec_id = str(exec_id or '').lower()
    if not valid_exec_id(exec_id):
        return unbound()
    recent = _recent_sessions(created_at)
    for _, data in recent:
        if _prompt_has_exec(data.get('id'), exec_id):
            return {'bound': 'exact', 'path': data['id']}
    repo_dir = _norm_dir(repo_cwd)
    window = [data for mtime, data in recent
              if mtime <= created_at + BIND_WINDOW_AFTER
              and _norm_dir(data.get('directory')) == repo_dir]
    if len(window) == 1:
        return {'bound': 'window', 'path': window[0]['id']}
    return unbound()


def tail(handle, offset=0, limit=None):
    """Replay a bound session from event cursor ``offset``. Events rebuild
    from the storage tree on each call (message time, then part id — the
    write order), so growth is picked up without byte-offset bookkeeping;
    the cursor counts emitted events and ``size`` the session's total event
    count (same units as the cursor, so ``offset >= size`` means drained —
    the client's exhaustion test), with ``bytes`` the part-file byte volume
    for the client's KB readout."""
    offset = max(0, int(offset or 0))
    limit = EVENT_LIMIT if limit is None else max(1, int(limit))
    events, skipped, bytes_ = [], 0, 0
    for message in _session_messages(str(handle or '')):
        for path in _json_files(Path(STORAGE_ROOT) / 'part'
                                / str(message.get('id') or '')):
            try:
                bytes_ += path.stat().st_size
            except OSError:
                pass
            part = _load(path)
            if part is None:
                skipped += 1
                continue
            event = _part_event(message, part)
            if event is None:
                skipped += 1
            else:
                events.append(event)
    window = events[offset:offset + limit]
    return {'events': window, 'offset': offset + len(window),
            'size': len(events), 'bytes': bytes_, 'skipped': skipped}
