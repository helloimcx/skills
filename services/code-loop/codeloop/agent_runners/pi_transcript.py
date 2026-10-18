"""The pi plugin's session reader (the output face).

pi persists a session as JSONL under
``~/.pi/agent/sessions/<munged-cwd>/<timestamp>_<uuid>.jsonl``, appended
entry by entry as the round runs. The first line is a ``type:"session"``
header carrying the exact ``cwd``, and the round's own prompt is the first
``role:"user"`` message — which is what makes the exact tier content-exact
while never trusting text the session merely read later (the 2026-09-18
cross-binding lesson: only the first user message decides). The directory
names are never decoded — candidates come from a bounded glob and are
matched by content, not by their mangled paths.

Same two tiers as the other file-backed readers: ``exact`` wins by content
(newest first, bounded scan), ``window`` only when exactly one recent
session matches the repo directory; anything else stays unbound. Parsing
is fail-soft: a malformed line is skipped (counted in ``skipped``), never
raised. ``tail()`` delegates the byte-cursor mechanics to the shared
``base.tail_jsonl``; this dialect only maps the entry shapes — ``message``
entries with ``text`` / ``thinking`` / ``toolCall`` blocks and separate
``toolResult`` messages — into the flat event contract. Monkeypatch target
for tests: ``SESSIONS_ROOT`` (module attribute).
"""
import json
import os
from pathlib import Path

from .base import (BIND_SLOP_BEFORE, BIND_WINDOW_AFTER, OUT_CAP, cap_text,
                   session_event, tail_jsonl, tool_text, ts, unbound,
                   valid_exec_id)

SESSIONS_ROOT = Path.home() / '.pi' / 'agent' / 'sessions'
SCAN_SESSIONS_MAX = 50   # binding reads at most this many newest sessions
SCAN_HEAD = 65536        # binding reads only the file head (prompt is first)


def _head_entries(path):
    """Parsed entries from the file head, stopping right after the first
    user message — the round's own prompt. Nothing later is ever binding
    evidence, no matter what the session went on to read or say."""
    try:
        with Path(path).open('rb') as handle:
            head = handle.read(SCAN_HEAD).decode('utf-8', errors='ignore')
    except OSError:
        return []
    entries = []
    for raw in head.splitlines():
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        entries.append(entry)
        message = entry.get('message')
        if (entry.get('type') == 'message' and isinstance(message, dict)
                and message.get('role') == 'user'):
            break
    return entries


def _user_texts(message):
    """The text of a user message — a plain string or typed text blocks."""
    content = message.get('content')
    if isinstance(content, str):
        return [content]
    return [str(block.get('text') or '') for block in content
            if isinstance(block, dict) and block.get('type') == 'text'] \
        if isinstance(content, list) else []


def _prompt_has_exec(path, exec_id):
    for entry in _head_entries(path):
        message = entry.get('message')
        if (entry.get('type') == 'message' and isinstance(message, dict)
                and message.get('role') == 'user'):
            return any(exec_id in text.lower() for text in _user_texts(message))
    return False


def _session_cwd(path):
    for entry in _head_entries(path):
        if entry.get('type') == 'session':
            return entry.get('cwd')
    return None


def _recent_sessions(created_at):
    """Newest-first recent sessions as ``(mtime, path)`` pairs, bounded —
    the scan is linear in a machine's session count, so it must stay capped."""
    pairs = []
    try:
        paths = [p for p in Path(SESSIONS_ROOT).glob('*/*.jsonl') if p.is_file()]
    except OSError:
        return pairs
    for path in paths:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= created_at - BIND_SLOP_BEFORE:
            pairs.append((mtime, path))
    pairs.sort(key=lambda pair: pair[0], reverse=True)
    return pairs[:SCAN_SESSIONS_MAX]


def resolve_session(repo_cwd, exec_id, created_at):
    """Bind an execution to a pi session file. Returns
    ``{'bound': 'exact'|'window'|None, 'path': str|None}`` — exact wins by
    content (the prompt embeds ``execution=<uuid>``), window only when
    exactly one recent session matches the repo directory."""
    exec_id = str(exec_id or '').lower()
    if not valid_exec_id(exec_id):
        return unbound()
    recent = _recent_sessions(created_at)
    for _, path in recent:
        if _prompt_has_exec(path, exec_id):
            return {'bound': 'exact', 'path': str(path)}
    repo_dir = os.path.normcase(os.path.normpath(str(repo_cwd or '')))
    window = []
    for mtime, path in recent:
        if mtime > created_at + BIND_WINDOW_AFTER:
            continue
        cwd = _session_cwd(path)
        if cwd and os.path.normcase(os.path.normpath(str(cwd))) == repo_dir:
            window.append(path)
    if len(window) == 1:
        return {'bound': 'window', 'path': str(window[0])}
    return unbound()


def _parse_line(obj, pending):
    """One session JSONL entry → zero or more flat events. ``pending``
    carries toolCall events keyed by id until their toolResult message
    arrives. Any unexpected shape degrades to zero events — never raises."""
    if not isinstance(obj, dict) or obj.get('type') != 'message':
        return []
    message = obj.get('message') if isinstance(obj.get('message'), dict) else {}
    role = message.get('role')
    t = ts(obj.get('timestamp')) or ts(message.get('timestamp'))
    if role == 'toolResult':
        return _tool_result_event(message, pending, t)
    blocks = message.get('content')
    if isinstance(blocks, str):
        text = blocks.strip()
        return [session_event(t, text)] if text else []
    events = []
    for block in blocks if isinstance(blocks, list) else ():
        events.extend(_block_event(block, t, pending))
    return events


def _block_event(block, t, pending):
    """One typed content block → zero or one flat event; anything unexpected
    degrades to zero."""
    if not isinstance(block, dict):
        return []
    kind = block.get('type')
    if kind == 'text':
        text = str(block.get('text') or '').strip()
        return [session_event(t, text)] if text else []
    if kind == 'thinking':
        text = str(block.get('thinking') or '').strip()
        return [session_event(t, text, fold=True)] if text else []
    if kind == 'toolCall':
        event = session_event(t, tool_text(str(block.get('name') or ''),
                                           block.get('arguments')),
                              mono=True)
        pending[str(block.get('id'))] = event
        return [event]
    return []


def _tool_result_event(message, pending, t):
    """Attach a toolResult to its pending toolCall event, or emit an
    output-only continuation when the pairing was lost across an offset."""
    result = message.get('content')
    if isinstance(result, list):
        result = '\n'.join(str(part.get('text') or '') for part in result
                           if isinstance(part, dict) and part.get('type') == 'text')
    out = cap_text(result, OUT_CAP) if isinstance(result, str) else None
    status = 'err' if message.get('isError') else None
    event = pending.pop(str(message.get('toolCallId')), None)
    if event is not None and event['out'] is None:
        event['out'] = out
        if status:
            event['status'] = status
        return []
    return [session_event(t, '', mono=True, out=out, status=status)]


def tail(handle, offset=0, limit=None):
    """Replay a bound session from byte cursor ``offset``; the mechanics are
    the shared ``base.tail_jsonl`` — this dialect only supplies its parser."""
    return tail_jsonl(str(handle or ''), offset, limit, _parse_line)
