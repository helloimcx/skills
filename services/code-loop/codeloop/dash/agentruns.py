"""Agent-run archives: deterministic derivation of proactively-spawned
subagent executions from the local CLI session stores, plus the per-run
session-stream reader the run-detail view consumes.

One adapter per CLI platform, registered in an explicit table (the
``agent_runners`` convention: discovery is explicit because a source that
failed to import must not vanish silently). Four sources ship: zcode
(subagent sessions are first-class child sessions in
``~/.zcode/cli/db/db.sqlite``), claude (Task tool_use/tool_result pairs in
the ``~/.claude/projects`` transcripts), opencode (the ``Task``-tool
sessions of ``~/.local/share/opencode/storage`` — v1 archives the main
session whose prompt carried the spawn, its storage tree replays the
process) and pi (sessions under ``~/.pi/agent/sessions`` — no subagent
concept in its storage, so v1 archives whole sessions whose **first user
message is a delegation-style prompt** and replays them directly). Every
read is fail-soft by contract: a missing store, a missing table, or a
malformed line degrades that source to zero rows — never an exception that
would take the collect cycle (or the run view) down. No platform write, no
LLM in the loop.

Monkeypatch targets for tests: ``ZCODE_DB``, ``CLAUDE_PROJECTS``,
``OPENCODE_STORAGE`` and ``PI_SESSIONS`` (module attributes).
Spec: docs/specs/2026-09-21-dash-execution-history.md.
"""
import json
import sqlite3
import time
from pathlib import Path

from ..agent_runners import opencode_storage, pi_transcript
from ..agent_runners.base import (OUT_CAP, session_event, tool_text,
                                  ts as _ts)
from .schema import AGENT_RUNS_RETENTION_DAYS, get_meta, set_meta

ZCODE_DB = Path.home() / '.zcode' / 'cli' / 'db' / 'db.sqlite'
CLAUDE_PROJECTS = Path.home() / '.claude' / 'projects'
OPENCODE_STORAGE = opencode_storage.STORAGE_ROOT   # tests patch BOTH this and
    # opencode_storage.STORAGE_ROOT (the reader reads its own module attribute)
PI_SESSIONS = pi_transcript.SESSIONS_ROOT

SWEEP_BATCH = 400          # max new runs derived per source per sweep
RUNNING_RESCAN_CAP = 200   # max live rows re-derived per sweep
STEP_HEAD_CAP = 200        # per-step input summary kept in the archive
RESULT_HEAD_CAP = 800      # result_head cap (the run view shows the stream)
LIVE_GRACE_SECONDS = 120   # a session touched this recently may still stream
SWEEP_OVERLAP_SECONDS = 60  # cursor overlap: re-derive this window every sweep
RETENTION_BUFFER = 86400   # retention cutoff slack (a day)

ZCODE_TOOL_CAP = 60        # steps derived from at most this many parts


def _ro(path):
    """Read-only sqlite connection; None when the store is missing."""
    if not Path(path).is_file():
        return None
    try:
        con = sqlite3.connect('file:%s?mode=ro' % Path(path).as_posix(), uri=True)
    except sqlite3.Error:
        return None
    con.row_factory = sqlite3.Row
    return con


def _cap(text, cap):
    text = str(text or '')
    return text[:cap] + '…' if len(text) > cap else text


def _head_cap(text, cap):
    """Single-line preview head: the list/table shows one truncated line."""
    return _cap(' '.join(str(text or '').split()), cap)


def _epoch(ms):
    try:
        value = float(ms) / 1000.0
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _duration(started, ended):
    if started is None or ended is None:
        return None
    return max(0, int(round(ended - started)))


# ---------------------------------------------------------------------------
# source registry (the agent_runners convention, one entry per CLI platform)

_SOURCES = {}


def register(source):
    """Add one session-source adapter. A duplicate id is a programming error
    and fails at import time, never silently overriding a source."""
    sid = getattr(source, 'id', None)
    if not isinstance(sid, str) or not sid:
        raise ValueError('SOURCE_INVALID: id must be a non-empty string')
    if sid in _SOURCES:
        raise ValueError('SOURCE_DUPLICATE: %s already registered' % sid)
    _SOURCES[sid] = source
    return source


def sources():
    return tuple(_SOURCES.values())


# ---------------------------------------------------------------------------
# zcode: subagent sessions are first-class child sessions
# (session.task_type='subagent_child', title = the task prompt, parts = the
# full process). The platform schema is an internal contract — every read is
# defensive and a missing table degrades this source to zero rows.

class ZcodeSource:
    id = 'zcode'
    label = 'zcode'

    @staticmethod
    def _connect():
        return _ro(ZCODE_DB)

    @staticmethod
    def enumerate(since):
        """New/updated child sessions after ``since`` (epoch seconds) →
        ``(run rows, cursor)``. A missing store yields ``([], since)`` — the
        cursor must not advance over a source that was never readable."""
        con = ZcodeSource._connect()
        if con is None:
            return [], since
        try:
            rows = con.execute(
                'SELECT id, parent_id, directory, title, time_created, time_updated '
                "FROM session WHERE task_type='subagent_child' AND time_updated>? "
                'ORDER BY time_updated LIMIT ?', (since * 1000.0, SWEEP_BATCH)).fetchall()
        except sqlite3.Error:
            return [], since
        finally:
            con.close()
        runs = [ZcodeSource.derive(row) for row in rows]
        now = time.time()
        if len(rows) == SWEEP_BATCH and runs:
            cursor = runs[-1]['ended_at'] or now   # batch full: continue at the edge
        else:
            # overlap window: updates committed after the SELECT snapshot are
            # re-derived next sweep (upsert is idempotent, so overlap is free)
            cursor = max(since, now - SWEEP_OVERLAP_SECONDS)
        return runs, cursor

    @staticmethod
    def refresh(running_rows):
        """Re-derive live rows so a RUNNING archive flips to its terminal
        state within one sweep of the session actually finishing."""
        if not running_rows:
            return []
        con = ZcodeSource._connect()
        if con is None:
            return []
        try:
            ids = [str(r['id']).split(':', 1)[-1]
                   for r in running_rows][:RUNNING_RESCAN_CAP]
            marks = ','.join('?' * len(ids))
            rows = con.execute(
                'SELECT id, parent_id, directory, title, time_created, time_updated '
                "FROM session WHERE task_type='subagent_child' AND id IN (%s)" % marks,
                ids).fetchall()
        except sqlite3.Error:
            return []
        finally:
            con.close()
        return [ZcodeSource.derive(row) for row in rows]

    @staticmethod
    def derive(row):
        """One session row → an agent_runs archive dict."""
        started, ended = _epoch(row['time_created']), _epoch(row['time_updated'])
        parts = ZcodeSource._parts(str(row['id']))
        state = 'RUNNING' if ZcodeSource._live(parts, ended) else 'SUCCEEDED'
        steps = [p for p in parts if p['kind'] == 'tool'][:ZCODE_TOOL_CAP]
        counts = {}
        for step in steps:
            counts[step['tool']] = counts.get(step['tool'], 0) + 1
        result = next((p['text'] for p in reversed(parts) if p['kind'] == 'text'), '')
        return {
            'id': 'zcode:%s' % row['id'], 'platform': 'zcode',
            'project': _head_cap(row['directory'], 120),
            'cwd': row['directory'], 'task_prompt': row['title'],
            'parent_ref': row['parent_id'], 'state': state,
            'started_at': started, 'ended_at': ended,
            'duration_s': _duration(started, ended),
            'steps': json.dumps(steps, ensure_ascii=False),
            'tool_counts': json.dumps(counts, ensure_ascii=False),
            'result_head': _cap(result, RESULT_HEAD_CAP),
            'transcript_ref': 'zcode db · session %s' % row['id'],
        }

    @staticmethod
    def _parts(session_id):
        con = ZcodeSource._connect()
        if con is None:
            return []
        try:
            rows = con.execute(
                'SELECT data, time_created, time_updated FROM part '
                'WHERE session_id=? ORDER BY id LIMIT ?',
                (session_id, SWEEP_BATCH * 4)).fetchall()
        except sqlite3.Error:
            return []
        finally:
            con.close()
        parts = []
        for row in rows:
            try:
                data = json.loads(row['data'])
            except (ValueError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            kind = data.get('type')
            if kind == 'tool':
                state = data.get('state') if isinstance(data.get('state'), dict) else {}
                parts.append({'kind': 'tool', 'tool': str(data.get('tool') or ''),
                              'input': state.get('input'),
                              'output': state.get('output') if isinstance(
                                  state.get('output'), str) else None,
                              'status': state.get('status'),
                              'at': _epoch(row['time_created'])})
            elif kind in ('text', 'reasoning'):
                text = str(data.get('text') or '').strip()
                if text:
                    parts.append({'kind': kind, 'text': text,
                                  'at': _epoch(row['time_created'])})
        return parts

    @staticmethod
    def _live(parts, ended):
        if any(p['status'] == 'running' for p in parts if p['kind'] == 'tool'):
            return True
        return ended is not None and time.time() - ended < LIVE_GRACE_SECONDS

    @staticmethod
    def session_events(run, since):
        """The run view's stream for a zcode archive: child-session parts →
        the flat event dict every consumer renders, incremental by part id."""
        con = ZcodeSource._connect()
        events, cursor, skipped = [], int(since or 0), 0
        if con is not None:
            # rowid is the cursor: part.id is TEXT in the wild, so it can
            # neither be compared nor parsed — rowid is always an integer
            # and monotonic with insert order.
            try:
                rows = con.execute(
                    'SELECT rowid AS rid, data, time_created FROM part '
                    'WHERE session_id=? AND rowid>? ORDER BY rowid LIMIT ?',
                    (str(run['id']).split(':', 1)[-1], int(since or 0),
                     SWEEP_BATCH * 4)).fetchall()
            except (sqlite3.Error, ValueError):
                rows = []
            finally:
                con.close()
            for row in rows:
                cursor = max(cursor, row['rid'])
                for event in ZcodeSource._part_events(row):
                    events.append(event)
        return {'bound': 'agent-session', 'path': run.get('transcript_ref'),
                'events': events, 'offset': cursor, 'size': cursor, 'skipped': skipped}

    @staticmethod
    def _part_events(row):
        try:
            data = json.loads(row['data'])
        except (ValueError, TypeError):
            return []
        if not isinstance(data, dict):
            return []
        kind, at = data.get('type'), _epoch(row['time_created'])
        if kind == 'tool':
            state = data.get('state') if isinstance(data.get('state'), dict) else {}
            failed = state.get('status') == 'error'
            out = state.get('output')
            return [session_event(at, tool_text(data.get('tool'), state.get('input')),
                                  mono=True,
                                  out=_cap(out, OUT_CAP) if isinstance(out, str) else None,
                                  status='err' if failed else None)]
        text = str(data.get('text') or '').strip()
        if text and kind in ('text', 'reasoning'):
            return [session_event(at, text, fold=(kind == 'reasoning'))]
        return []


register(ZcodeSource())


# ---------------------------------------------------------------------------
# claude: Task tool_use/tool_result pairs in the main-session transcripts
# (the subagent's own sidechain file has no stable, documented linkage to the
# parent's tool_use id, so v1 archives the pair deterministically and the run
# view degrades to timeline-only for claude — an honest, bounded gap).

TASK_TOOLS = ('task', 'agent')


class ClaudeSource:
    id = 'claude'
    label = 'claude'

    @staticmethod
    def enumerate(since):
        if not Path(CLAUDE_PROJECTS).is_dir():
            return [], since
        now = time.time()
        newest, runs = since, []
        try:
            files = sorted(Path(CLAUDE_PROJECTS).glob('*/*.jsonl'),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return [], since
        for path in files:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime <= since - 60 or len(runs) >= SWEEP_BATCH:
                break            # sorted newest-first: older files cannot qualify
            newest = max(newest, mtime)
            runs.extend(ClaudeSource._scan(path))
        return runs, max(newest, since, now - SWEEP_OVERLAP_SECONDS)

    @staticmethod
    def refresh(running_rows):
        """A claude archive is RUNNING while its tool_use has no tool_result;
        re-scan just those transcripts for the pairing."""
        runs = []
        for row in running_rows[:RUNNING_RESCAN_CAP]:
            ref = str(row['transcript_ref'] or '')
            path, _, tool_use_id = ref.rpartition('#')
            if not tool_use_id:
                continue
            runs.extend(pair for pair in ClaudeSource._scan(Path(path))
                        if pair['id'] == row['id'])
        return runs

    @staticmethod
    def _scan(path):
        """One transcript → the Task-pair archives it contains. Malformed
        lines are skipped, never raised (PR#129 lesson); the file is streamed
        line by line (first sweeps must not load whole history in memory)."""
        pending, runs = {}, []
        try:
            handle = Path(path).open('r', encoding='utf-8', errors='ignore')
        except OSError:
            return []
        with handle:
            for line in handle:
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                ClaudeSource._line(obj, path, pending, runs)
        return runs

    @staticmethod
    def _line(obj, path, pending, runs):
        blocks = ClaudeSource._blocks(obj)
        at = _epoch_ms(obj.get('timestamp'))
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'tool_use' \
                    and str(block.get('name') or '').lower() in TASK_TOOLS:
                bid = str(block.get('id') or '')
                pending[bid] = ClaudeSource._pending_run(path, block, at)
            elif block.get('type') == 'tool_result' \
                    and str(block.get('tool_use_id') or '') in pending:
                ClaudeSource._finish(pending.pop(str(block['tool_use_id'])),
                                     block, at, runs)

    @staticmethod
    def _blocks(obj):
        message = obj.get('message')
        content = message.get('content') if isinstance(message, dict) else None
        return content if isinstance(content, list) else []

    @staticmethod
    def _pending_run(path, block, at):
        tool_input = block.get('input') if isinstance(block.get('input'), dict) else {}
        prompt = tool_input.get('prompt') or tool_input.get('description') or ''
        cwd, project = ClaudeSource._head_cwd(path)
        return {
            'id': 'claude:%s#%s' % (path.stem, block.get('id')),
            'platform': 'claude', 'project': project,
            'cwd': cwd, 'task_prompt': str(prompt),
            'parent_ref': path.stem, 'state': 'RUNNING',
            'started_at': at, 'ended_at': None, 'duration_s': None,
            'steps': None, 'tool_counts': None, 'result_head': '',
            'transcript_ref': '%s#%s' % (path, block.get('id')),
        }

    @staticmethod
    def _finish(run, block, at, runs):
        result = block.get('content')
        if isinstance(result, list):
            result = '\n'.join(part.get('text', '') for part in result
                               if isinstance(part, dict) and part.get('type') == 'text')
        run.update({'state': 'SUCCEEDED', 'ended_at': at,
                    'duration_s': _duration(run['started_at'], at),
                    'result_head': _cap(result, RESULT_HEAD_CAP)})
        runs.append(run)

    @staticmethod
    def _head_cwd(path):
        """(cwd, project_label) from the transcript head: the cwd recorded in
        the first lines, else ('', the munged directory name claude gave the
        folder)."""
        try:
            with Path(path).open('r', encoding='utf-8', errors='ignore') as handle:
                for _ in range(10):
                    line = handle.readline()
                    if not line:
                        break
                    try:
                        obj = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    cwd = obj.get('cwd')
                    if isinstance(cwd, str) and cwd:
                        return cwd, _head_cap(cwd, 120)
        except OSError:
            pass
        return '', _head_cap(path.parent.name, 120)

    @staticmethod
    def session_events(run, since):
        """No stable sidechain linkage in v1: the run view degrades to the
        deterministic timeline for claude archives (Spec Non-goal). The
        placeholder carries the full key set the run view consumes — the
        same shape runapi's unbound placeholder uses."""
        since = int(since or 0)
        return {'bound': None, 'path': None, 'events': [],
                'offset': since, 'size': since, 'skipped': 0}


register(ClaudeSource())


# ---------------------------------------------------------------------------
# opencode: sessions live as JSON trees under ~/.local/share/opencode/storage
# (session/message/part). There is no subagent marker in the storage, so the
# source archives the sessions whose first user text embeds the code-loop
# spawn markers — those are exactly the headless rounds a loop/trigger/board
# spawned (the interactive sessions never carry them). Streams replay through
# the shared opencode_storage reader, so binding and the run view share one
# dialect parser.

SPAWN_MARKERS = ('execution=', 'token=')


class OpencodeSource:
    id = 'opencode'
    label = 'opencode'

    @staticmethod
    def _session_files():
        try:
            return [p for p in Path(OPENCODE_STORAGE).glob('session/*/*.json')
                    if p.is_file()]
        except OSError:
            return []

    @staticmethod
    def _first_user_text(session_id):
        """The spawned prompt: the first user message's text parts."""
        for message in opencode_storage._session_messages(session_id):
            if message.get('role') != 'user':
                continue
            texts = []
            for part in opencode_storage._session_parts(message.get('id')):
                if part.get('type') == 'text':
                    texts.append(str(part.get('text') or ''))
            return '\n'.join(texts)
        return ''

    @staticmethod
    def _tool_steps(session_id):
        """The session's tool calls as archive steps (bounded)."""
        parts = []
        for message in opencode_storage._session_messages(session_id):
            for part in opencode_storage._session_parts(message.get('id')):
                if part.get('type') != 'tool':
                    continue
                state = part.get('state') if isinstance(part.get('state'), dict) else {}
                tool_input = state.get('input') if isinstance(
                    state.get('input'), dict) else {}
                parts.append({'tool': str(part.get('tool') or ''),
                              'input': tool_input.get('command') or state.get('title'),
                              'status': state.get('status')})
                if len(parts) >= ZCODE_TOOL_CAP:
                    return parts
        return parts

    @staticmethod
    def _derive(path, data, prompt):
        directory = data.get('directory')
        times = data.get('time') or {}
        started = opencode_storage._ms(times.get('created'))
        ended = opencode_storage._ms(times.get('updated')) or None
        parts = OpencodeSource._tool_steps(data['id'])
        counts = {}
        for step in parts:
            counts[step['tool']] = counts.get(step['tool'], 0) + 1
        live = ended is None or time.time() - ended < LIVE_GRACE_SECONDS
        return {
            'id': 'opencode:%s' % data['id'], 'platform': 'opencode',
            'project': _head_cap(directory, 120), 'cwd': directory,
            'task_prompt': _cap(prompt, 8000),
            'parent_ref': '', 'state': 'RUNNING' if live else 'SUCCEEDED',
            'started_at': started, 'ended_at': ended,
            'duration_s': _duration(started, ended),
            'steps': json.dumps(parts, ensure_ascii=False),
            'tool_counts': json.dumps(counts, ensure_ascii=False),
            'result_head': '',
            'transcript_ref': 'opencode storage · session %s' % data['id'],
        }

    @staticmethod
    def enumerate(since):
        now = time.time()
        runs, cursor = [], since
        for path in OpencodeSource._session_files():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            cursor = max(cursor, mtime)
            if mtime <= since - SWEEP_OVERLAP_SECONDS or len(runs) >= SWEEP_BATCH:
                continue
            data = opencode_storage._load(path)
            if not isinstance(data, dict) or not data.get('id'):
                continue
            prompt = OpencodeSource._first_user_text(data['id'])
            lowered = prompt.lower()
            if any(marker in lowered for marker in SPAWN_MARKERS):
                runs.append(OpencodeSource._derive(path, data, prompt))
        return runs, (max(cursor, now - SWEEP_OVERLAP_SECONDS) if runs else now)

    @staticmethod
    def refresh(running_rows):
        """Re-derive the live rows from their session files (same derive)."""
        runs = []
        for row in running_rows[:RUNNING_RESCAN_CAP]:
            sid = str(row['id']).split(':', 1)[-1]
            matches = [p for p in OpencodeSource._session_files()
                       if p.stem == sid]
            data = opencode_storage._load(matches[0]) if matches else None
            if isinstance(data, dict) and data.get('id') == sid:
                prompt = OpencodeSource._first_user_text(sid)
                runs.append(OpencodeSource._derive(None, data, prompt))
        return runs

    @staticmethod
    def session_events(run, since):
        """Replay through the shared opencode reader (event-count cursor),
        wrapped into the run view's session shape."""
        stream = opencode_storage.tail(str(run['id']).split(':', 1)[-1], since)
        return dict(stream, bound='agent-session',
                    path=run.get('transcript_ref'))


register(OpencodeSource())


# ---------------------------------------------------------------------------
# pi: sessions are JSONL files under ~/.pi/agent/sessions/<munged-cwd>/.
# pi's storage has no subagent concept, so v1 archives the headless rounds
# themselves — sessions whose first user message carries the code-loop spawn
# markers (the same rule the opencode source uses) — and replays them
# directly through the shared pi reader. The id is the file stem, so the run
# view's deep link stays stable across sweeps.

class PiSource:
    id = 'pi'
    label = 'pi'

    @staticmethod
    def _session_files():
        try:
            return [p for p in Path(PI_SESSIONS).glob('*/*.jsonl') if p.is_file()]
        except OSError:
            return []

    @staticmethod
    def _prompt(path):
        """The delegation prompt: the first user message's text."""
        for entry in pi_transcript._head_entries(path):
            message = entry.get('message')
            if (entry.get('type') == 'message' and isinstance(message, dict)
                    and message.get('role') == 'user'):
                return '\n'.join(pi_transcript._user_texts(message))
        return ''

    @staticmethod
    def _tool_steps(path):
        """Bounded scan of the session's toolCall blocks → (steps, counts).
        The stream is the file itself; this only fills the list summary."""
        steps, counts = [], {}
        try:
            with path.open('r', encoding='utf-8', errors='ignore') as handle:
                for line in handle:
                    try:
                        entry = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    message = entry.get('message') if isinstance(
                        entry.get('message'), dict) else {}
                    for block in message.get('content') or []:
                        if isinstance(block, dict) and block.get('type') == 'toolCall':
                            tool = str(block.get('name') or '')
                            steps.append({'tool': tool, 'input': '', 'status': ''})
                            counts[tool] = counts.get(tool, 0) + 1
                    if len(steps) >= ZCODE_TOOL_CAP:
                        return steps, counts
        except OSError:
            pass
        return steps, counts

    @staticmethod
    def _derive(path):
        stem = path.stem
        prompt = PiSource._prompt(path)
        cwd = pi_transcript._session_cwd(path)
        started = _ts(path.stem.split('_', 1)[0].replace('T', ' ')) \
            if '_' in stem else None
        try:
            ended = path.stat().st_mtime
        except OSError:
            return None
        live = time.time() - ended < LIVE_GRACE_SECONDS
        steps, counts = PiSource._tool_steps(path)
        return {
            'id': 'pi:%s' % stem, 'platform': 'pi',
            'project': _head_cap(cwd, 120), 'cwd': cwd or '',
            'task_prompt': _cap(prompt, 8000),
            'parent_ref': '', 'state': 'RUNNING' if live else 'SUCCEEDED',
            'started_at': started, 'ended_at': ended,
            'duration_s': _duration(started, ended),
            'steps': json.dumps(steps, ensure_ascii=False),
            'tool_counts': json.dumps(counts, ensure_ascii=False),
            'result_head': '',
            'transcript_ref': str(path),
        }

    @staticmethod
    def enumerate(since):
        runs, cursor = [], since
        for path in PiSource._session_files():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            cursor = max(cursor, mtime)
            if mtime <= since - SWEEP_OVERLAP_SECONDS or len(runs) >= SWEEP_BATCH:
                continue
            lowered = ''
            for entry in pi_transcript._head_entries(path):
                message = entry.get('message')
                if (entry.get('type') == 'message' and isinstance(message, dict)
                        and message.get('role') == 'user'):
                    lowered = '\n'.join(pi_transcript._user_texts(message)).lower()
                    break
            if any(marker in lowered for marker in SPAWN_MARKERS):
                run = PiSource._derive(path)
                if run:
                    runs.append(run)
        return runs, (max(cursor, time.time() - SWEEP_OVERLAP_SECONDS)
                      if runs else time.time())

    @staticmethod
    def refresh(running_rows):
        runs = []
        for row in running_rows[:RUNNING_RESCAN_CAP]:
            stem = str(row['id']).split(':', 1)[-1]
            matches = [p for p in PiSource._session_files() if p.stem == stem]
            if matches:
                run = PiSource._derive(matches[0])
                if run:
                    runs.append(run)
        return runs

    @staticmethod
    def session_events(run, since):
        """Replay through the shared pi reader (byte cursor, its dialect),
        wrapped into the run view's session shape."""
        stream = pi_transcript.tail(run.get('transcript_ref') or '', since)
        return dict(stream, bound='agent-session',
                    path=run.get('transcript_ref'))


register(PiSource())


# ---------------------------------------------------------------------------
# sweep + stream dispatch

def _epoch_ms(value):
    """Transcript stamps: ISO-8601 or numeric epoch-ms → epoch seconds
    (the shared runner ``ts`` conversion, re-exported under a local name)."""
    return _ts(value)


def _upsert_run(db, run):
    db.execute('''
        INSERT INTO agent_runs (id, platform, project, cwd, task_prompt, parent_ref,
                                state, started_at, ended_at, duration_s,
                                steps, tool_counts, result_head, transcript_ref,
                                first_seen_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            platform=excluded.platform, project=excluded.project, cwd=excluded.cwd,
            task_prompt=excluded.task_prompt, parent_ref=excluded.parent_ref,
            state=excluded.state, started_at=excluded.started_at,
            ended_at=excluded.ended_at, duration_s=excluded.duration_s,
            steps=excluded.steps, tool_counts=excluded.tool_counts,
            result_head=excluded.result_head, transcript_ref=excluded.transcript_ref
    ''', (run['id'], run['platform'], run['project'], run['cwd'], run['task_prompt'],
          run['parent_ref'], run['state'], run['started_at'], run['ended_at'],
          run['duration_s'], run['steps'], run['tool_counts'], run['result_head'],
          run['transcript_ref'], time.time()))


def _running_rows(db, platform):
    return db.execute("SELECT * FROM agent_runs WHERE platform=? AND state='RUNNING'",
                      (platform,)).fetchall()


def sweep(db, now=None):
    """Derive/refresh every source's archives into ``agent_runs`` and age out
    the retention window. Single-source failure is recorded and never stops
    the other sources or the collect cycle. Returns per-source notes."""
    now = time.time() if now is None else now
    notes = {}
    for src in sources():
        key = 'agentruns.cursor.%s' % src.id
        since = float(get_meta(db, key) or 0)
        try:
            runs, cursor = src.enumerate(since)
            runs.extend(src.refresh(_running_rows(db, src.id)))
            with db:
                for run in runs:
                    _upsert_run(db, run)
                db.execute('DELETE FROM agent_runs WHERE platform=? AND state<>\'RUNNING\''
                           ' AND started_at<?',
                           (src.id, now - AGENT_RUNS_RETENTION_DAYS * 86400
                            - RETENTION_BUFFER))
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
            notes[src.id] = 'error: %s' % exc
            continue
        if cursor > since:
            set_meta(db, key, repr(float(cursor)))
        notes[src.id] = '%d run(s)' % len(runs)
    return notes


def session_events(run, since):
    """The run view's stream dispatch: the archive's platform adapter reads
    its own storage. An unregistered platform degrades to the unbound
    placeholder (timeline-only), exactly like an unbound loop round."""
    run = dict(run)
    src = _SOURCES.get(run.get('platform'))
    if src is None:
        since = int(since or 0)
        return {'bound': None, 'path': None, 'events': [],
                'offset': since, 'size': since, 'skipped': 0}
    return src.session_events(run, since)
