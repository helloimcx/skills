"""The claude plugin's transcript reader (the output face).

A spawned worker round writes a JSONL transcript under
``~/.claude/projects/<munged-cwd>/<session-uuid>.jsonl``; the dashboard
manual-review prompt embeds ``execution=<uuid>`` in its first line, which
gives a content-exact execution→session binding (the ``exact`` tier). Rounds
whose prompt lacks the id (cron rounds) can only be guessed from a
time-window, and only when exactly one candidate file exists (``window``);
anything else stays unbound and the view falls back to the deterministic
control-plane timeline.

Parsing is fail-soft by contract: malformed or unknown lines are skipped
(counted in ``skipped``), never raised. Events come from
``base.session_event`` — the flat, full-content, unlabeled shape every
consumer already renders. ``tail()`` resumes from a byte ``offset`` and
holds back an incomplete last line, so a growing transcript is polled
without repeats or losses — the mechanics are the shared ``base.tail_jsonl``.
Monkeypatch target for tests: ``PROJECTS_ROOT`` (module attribute).
"""
import re
from pathlib import Path

from .base import (BIND_SLOP_BEFORE, BIND_WINDOW_AFTER, CHUNK_MAX, OUT_CAP,
                   cap_text, session_event, tail_jsonl, tool_text, ts,
                   unbound, valid_exec_id)

PROJECTS_ROOT = Path.home() / '.claude' / 'projects'

SCAN_HEAD = 65536           # binding reads only the file head (prompt is first)
SCAN_LINES = 5              # ...and exact matching only the first messages:
                            # the id must come from the round's own prompt,
                            # never from text the session happened to read
                            # later (2026-09-18 cross-binding)


def munged(repo_cwd):
    """Claude CLI's transcript directory name for a cwd (observed:
    ``D:\\ai\\skills`` → ``D--ai-skills``, ``D:\\ai\\ai_quality_analysis`` →
    ``D--ai-ai-quality-analysis``): every non-alphanumeric byte becomes ``-``."""
    return re.sub(r'[^A-Za-z0-9]', '-', str(repo_cwd))


def parse_line(obj, pending):
    """One stream JSON object → zero or more flat events. ``pending`` carries
    tool_use events keyed by id until their tool_result arrives.

    Two CLI dialects share this reader because their type vocabularies are
    disjoint: claude's transcript uses ``assistant``/``user`` (blocks inside
    ``message.content``), while ``opencode run --format json`` emits one event
    per line — ``text``, ``tool_use``, and the ``step_start``/``step_finish``
    accounting pair. Dispatch is per line, so a file needs no format sniffing.
    Any unexpected shape degrades to zero events — never raises."""
    if not isinstance(obj, dict):
        return []
    kind = obj.get('type')
    if kind == 'assistant':
        return _parse_assistant(obj, pending)
    if kind == 'user':
        return _parse_user(obj, pending)
    if kind in ('text', 'reasoning', 'thinking', 'tool_use'):
        return _parse_opencode(obj, kind)
    return []   # step_start / step_finish: token accounting, not progress


def _part_of(obj):
    part = obj.get('part')
    return part if isinstance(part, dict) else {}


def _parse_opencode(obj, kind):
    """One line of the ``--format json`` event stream. Tool results arrive
    inline (``part.state``) rather than as a separate message, so nothing is
    held in ``pending``."""
    part = _part_of(obj)
    t = ts(obj.get('timestamp'))
    if kind == 'tool_use':
        return [_opencode_tool_event(t, part)]
    text = str(part.get('text') or part.get('reasoning') or '').strip()
    if not text:
        return []
    return [session_event(t, text, fold=(kind != 'text'))]


def _opencode_tool_event(t, part):
    """A tool call with its inline result: the same monospace line and output
    attachment the transcript dialect produces, with a non-zero exit or an
    errored state surfacing as ``status='err'``."""
    state = part.get('state') if isinstance(part.get('state'), dict) else {}
    metadata = state.get('metadata') if isinstance(state.get('metadata'), dict) else {}
    exit_code = metadata.get('exit')
    failed = state.get('status') == 'error' or (
        isinstance(exit_code, int) and not isinstance(exit_code, bool)
        and exit_code != 0)
    raw_out = state.get('output')
    return session_event(t, tool_text(part.get('tool'), state.get('input')),
                  mono=True,
                  out=cap_text(raw_out, OUT_CAP) if isinstance(raw_out, str) else None,
                  status='err' if failed else None)


def _blocks_of(obj):
    """The message content block list, or () for anything unexpected — the
    transcript is a foreign CLI's storage contract, never trusted."""
    message = obj.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    return content if isinstance(content, list) else ()


def _parse_assistant(obj, pending):
    events = []
    for block in _blocks_of(obj):
        if not isinstance(block, dict):
            continue
        t = ts(obj.get('timestamp'))
        btype = block.get('type')
        if btype == 'thinking':
            text = str(block.get('thinking') or '').strip()
            if text:
                events.append(session_event(t, text, fold=True))
        elif btype == 'text':
            text = str(block.get('text') or '').strip()
            if text:
                events.append(session_event(t, text))
        elif btype == 'tool_use':
            event = session_event(t, tool_text(block.get('name'), block.get('input')),
                           mono=True)
            pending[str(block.get('id'))] = event
            events.append(event)
    return events


def _parse_user(obj, pending):
    content = (obj.get('message') or {}).get('content') \
        if isinstance(obj.get('message'), dict) else None
    t = ts(obj.get('timestamp'))
    if isinstance(content, str):
        text = content.strip()
        return [session_event(t, text)] if text else []
    events = []
    for block in content if isinstance(content, list) else ():
        if not isinstance(block, dict) or block.get('type') != 'tool_result':
            continue
        _apply_tool_result(block, pending, events, t)
    return events


def _apply_tool_result(block, pending, events, t):
    """Attach a tool_result to its pending tool_use event, or emit an
    output-only continuation when the pairing was lost across an offset."""
    result = block.get('content')
    if isinstance(result, list):
        result = '\n'.join(part.get('text', '') for part in result
                           if isinstance(part, dict) and part.get('type') == 'text')
    out, status = cap_text(result, OUT_CAP), ('err' if block.get('is_error') else None)
    event = pending.pop(str(block.get('tool_use_id')), None)
    if event is not None and event['out'] is None:
        event['out'] = out
        if status:
            event['status'] = status
    else:
        events.append(session_event(t, '', mono=True, out=out, status=status))


def tail(path, offset=0, limit=None):
    """Incrementally parse a transcript from byte ``offset``; returns
    ``{'events', 'offset', 'size', 'skipped'}``. An incomplete last line is
    held back (the offset stops before it) so a live file polls cleanly.
    ``limit=None`` means the reader default (the plugin calling convention).
    The cursor mechanics are the shared ``base.tail_jsonl`` — this dialect
    only supplies its line parser."""
    return tail_jsonl(path, offset, limit, parse_line)


def resolve_session(repo_cwd, exec_id, created_at):
    """Bind an execution to a transcript file. Returns
    ``{'bound': 'exact'|'window'|None, 'path': str|None}`` — exact wins by
    content (newest first), window only when the recent set is a singleton."""
    exec_id = str(exec_id or '').lower()
    if not valid_exec_id(exec_id):
        return unbound()
    directory = Path(PROJECTS_ROOT) / munged(repo_cwd)
    try:
        candidates = [p for p in Path(directory).glob('*.jsonl') if p.is_file()]
    except OSError:
        candidates = []
    recent = []
    for path in candidates:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= created_at - BIND_SLOP_BEFORE:
            recent.append((mtime, path))
    recent.sort(key=lambda pair: pair[0], reverse=True)
    for _, path in recent:
        try:
            with path.open('rb') as handle:
                head = handle.read(SCAN_HEAD).decode('utf-8', errors='ignore')
        except OSError:
            continue
        opening = '\n'.join(head.splitlines()[:SCAN_LINES]).lower()
        if exec_id in opening:
            return {'bound': 'exact', 'path': str(path)}
    window = [pair for pair in recent if pair[0] <= created_at + BIND_WINDOW_AFTER]
    if len(window) == 1:
        return {'bound': 'window', 'path': str(window[0][1])}
    return unbound()
