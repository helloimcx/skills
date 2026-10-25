"""The coding-agent plugin interface.

One plugin answers four questions, and each belongs to a different machine:

* **identity** — ``id``: the token an operator's click sends and the control
  plane whitelists. Server-side, behavior-free.
* **capability** — ``models_command`` / ``models()``: what this CLI offers,
  asked on the dashboard host only to fill the trigger dialog's dropdown.
* **argv** — ``argv(model)``: the command line to spawn, resolved on the
  machine that pays for the session.
* **output** — ``resolve_session`` / ``tail``: where this CLI writes a
  session's execution trace on the machine that ran it, parsed into the flat
  event dict every consumer already renders.

Keep them apart. The dropdown can only ever reflect the host's CLI,
while the verdict belongs to the executing machine, which re-checks a
requested model against its own list before spawning. A plugin that merged
them would collapse that two-stage defence.

Everything here is pure allow-list parsing of a deterministic CLI: model
identifiers are charset-checked before they ride a command frame, and they
pick an argv preset in executor code — they never reach a prompt (doorbell,
not envelope). The output face is equally trust-bounded: it only ever reads
the local files a session left behind, never writes.

The output face's shared contract lives below: the flat event dict (``t``,
``text``, ``out``, ``mono``, ``fold``, ``status``), its capacity caps, the
execution→session binding window, the npm-shim skeleton behind the plugins'
native-binding policies, and the file-backed JSONL replay (``tail_jsonl``).
What stays plugin-private is the cursor's meaning — a file-backed reader's
resume cursor is a byte offset, a storage-backed one's is an event count;
both consumers treat the cursor as opaque and the ``size`` readout as
content volume.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from ..cli import resolve_command

MODEL_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$')
DEFAULT_MODELS_TIMEOUT = 30.0

# -- output face: the shared event contract ----------------------------------
TEXT_CAP = 16384   # a single event's text is capped (fold + scroll handle the rest)
OUT_CAP = 4000     # tool output attached to an event
EVENT_LIMIT = 400  # max events per tail() call; the client resumes by cursor
BIND_SLOP_BEFORE = 120      # mtime clock slop before the execution started
BIND_WINDOW_AFTER = 600     # window-binding upper bound after the start
EXEC_ID_RE = re.compile(r'^[0-9a-f]{32}$')
CHUNK_MAX = 8 * 1024 * 1024

# -- npm shim skeleton (the plugins' native-binding policies filter these) ---
SHIM_SUFFIXES = ('.cmd', '.bat')
SHIM_QUOTED_RE = re.compile(r'"([^"]*?)"')
SHIM_DP0_RE = re.compile(r'%~?dp0%?\\?', re.IGNORECASE)


def valid_exec_id(value):
    return isinstance(value, str) and bool(EXEC_ID_RE.fullmatch(value))


def cap_text(text, cap):
    text = str(text or '')
    return text[:cap - 1] + '…' if len(text) > cap else text


def session_event(t, text, mono=False, fold=False, out=None, status=None):
    """One flat event: full content, no category labels — thinking, tool
    calls and answers all stream in time order; the only signals are
    ``mono`` (tool line renders monospace), ``fold`` (overlong thinking
    content collapses to one preview line) and ``status: 'err'``."""
    return {'t': t, 'text': cap_text(text, TEXT_CAP), 'out': out, 'mono': mono,
            'fold': fold, 'status': status}


def unbound():
    """The honest empty binding: no session output known for this execution."""
    return {'bound': None, 'path': None}


def ts(value):
    """ISO-8601 Z timestamp → epoch seconds; None when absent/malformed.
    Numeric stamps are epoch milliseconds — the other clock the CLI dialects
    use. Every conversion stays fail-soft: a hostile or absurd value is a
    missing timestamp, never an exception that would take the stream down."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return float(value) / 1000.0
        except OverflowError:
            return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def tool_text(name, tool_input):
    """One command-like line for a tool call (monospace rendering)."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    command = tool_input.get('command')
    if isinstance(command, str) and command:
        return '$ ' + command
    for key in ('file_path', 'path', 'notebook_path'):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return '%s %s' % (name, value)
    pattern = tool_input.get('pattern')
    if isinstance(pattern, str) and pattern:
        return '%s %s' % (name, pattern)
    try:
        rest = json.dumps(tool_input, ensure_ascii=False)
    except (TypeError, ValueError):
        rest = ''
    return ('%s %s' % (name, rest)).strip()


def tail_jsonl(path, offset=0, limit=None, parse_line=None):
    """Incremental JSONL replay shared by the file-backed session readers: a
    byte cursor that holds back an incomplete last line (a growing transcript
    polls cleanly), and — issue #195 — a continuation read when the chunk
    ends mid-line, so a single line larger than one chunk cannot dead-lock
    the offset. ``parse_line(obj, pending)`` maps one JSON object to zero or
    more flat events, with ``pending`` carrying tool calls until their result
    arrives; malformed lines count as skipped, never raise."""
    limit = EVENT_LIMIT if limit is None else max(1, int(limit))
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return {'events': [], 'offset': max(0, int(offset or 0)), 'size': 0,
                'skipped': 0}
    offset = max(0, min(int(offset or 0), size))
    if offset >= size:
        return {'events': [], 'offset': offset, 'size': size, 'skipped': 0}
    try:
        with path.open('rb') as handle:
            handle.seek(offset)
            chunk = handle.read(min(CHUNK_MAX, size - offset))
            while chunk and not chunk.endswith(b'\n'):
                more = handle.read(CHUNK_MAX)
                if not more:
                    break
                chunk += more
    except OSError:
        return {'events': [], 'offset': offset, 'size': size, 'skipped': 0}
    events, pending = [], {}
    consumed, skipped = 0, 0
    for raw in chunk.split(b'\n')[:-1]:   # last piece is incomplete (or empty)
        if len(events) >= limit:
            break
        consumed += len(raw) + 1
        try:
            before = len(pending)
            produced = parse_line(json.loads(raw.decode('utf-8', errors='replace')),
                                  pending)
        except (ValueError, TypeError, AttributeError, OverflowError):
            produced, before = [], len(pending)
        # A line may yield several events (parallel tool_results): it goes out
        # whole even past the limit — truncating it would silently lose events
        # while the offset moves past their bytes.
        events.extend(produced)
        if not produced and len(pending) == before:
            # no event and no pending tool call filled: a genuinely skipped line
            skipped += 1
    return {'events': events, 'offset': offset + consumed, 'size': size,
            'skipped': skipped}


def valid_model_id(value):
    return (isinstance(value, str)
            and bool(MODEL_ID_RE.fullmatch(value)))


def shim_targets(path, read_fn=None):
    """Every quoted target inside an npm ``.cmd``/``.bat`` shim that resolves
    to a real file, in order of appearance. This is the skeleton the plugins'
    native-binding policies filter: opencode adopts the first ``.exe`` (its
    install is a native binary), pi the first ``.js`` plus a node
    interpreter (its install is a script). Existence is the only trust: a
    literal like ``%_prog%`` in the shim body never resolves to a file, and
    a target is only adopted when it exists. Reading is fail-soft — an
    unreadable shim yields no targets, never a raise."""
    read = read_fn or (lambda p: Path(p).read_text(encoding='utf-8',
                                                   errors='replace'))
    try:
        body = read(str(path))
    except OSError:
        return []
    directory = str(Path(path).parent)
    targets = []
    for raw in SHIM_QUOTED_RE.findall(body or ''):
        candidate = SHIM_DP0_RE.sub(lambda _m: directory + os.sep, raw)
        norm = os.path.normpath(candidate.replace('\\', os.sep))
        target_path = Path(norm) if Path(norm).is_file() else Path(candidate)
        if target_path.is_file():
            found = str(target_path)
            if found not in targets:
                targets.append(found)
    return targets


def detect_models(command, run_fn=None, which_fn=None, timeout=DEFAULT_MODELS_TIMEOUT):
    """Run a CLI's model listing and return its provider/model tokens. Any
    failure — missing binary, timeout, non-zero exit — is an empty list: a
    degraded dropdown must not break the endpoint that serves it."""
    resolved = which_fn or shutil.which
    if not resolved(command[0]):
        return []
    argv = resolve_command(command, which_fn=resolved)
    run = run_fn or subprocess.run
    try:
        proc = run(argv, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    stdout = proc.stdout or ''
    if isinstance(stdout, bytes):
        stdout = stdout.decode('utf-8', errors='replace')
    models = []
    for line in stdout.splitlines():
        token = line.strip()
        if valid_model_id(token) and token not in models:
            models.append(token)
    return models


class Runner:
    """One coding-agent plugin. Subclasses declare their identity and the two
    machine-local answers; the registry does the dispatch."""

    #: Command-frame token: operator-visible, whitelisted by the control plane.
    id = ''
    #: Dialog display text (the label is the card title, the sub its caption).
    label = ''
    sub = ''
    #: The CLI's model-listing argv, or None when it has none — a CLI without
    #: a listing command gets no picker, and a model sent next to it is a
    #: mistake that is refused loudly rather than silently dropped.
    models_command = None
    #: Only runtimes with native child-agent orchestration can be registered
    #: for the worker Coordinator path. The CLI remains per-task, not resident.
    supports_subagents = False

    @property
    def has_models(self):
        return self.models_command is not None

    def models(self, run_fn=None, which_fn=None, timeout=DEFAULT_MODELS_TIMEOUT):
        """Detected models for the dialog, or None when this CLI has no
        listing command (None is not an empty list: the dialog must tell
        "cannot be picked" apart from "detection found nothing")."""
        if self.models_command is None:
            return None
        return detect_models(self.models_command, run_fn=run_fn,
                             which_fn=which_fn, timeout=timeout)

    def argv(self, model=None, which_fn=None, run_fn=None):
        """The argv preset for this plugin, or None meaning "use the machine's
        registered spawn command". Raises ValueError with a code-shaped
        message the UI surfaces verbatim (AGENT_MISSING / MODEL_UNAVAILABLE /
        INVALID_COMMAND) — the executor's own gate, ahead of any claim."""
        raise NotImplementedError

    def coordinator_flags(self, task_type=None):
        """Runtime CLI flags that keep a Coordinator within its task role."""
        return []

    def coordinator_environment(self, task_type=None):
        """Per-process runtime configuration for a scoped Coordinator."""
        return None

    def coordinator_argv(self, model=None, task_type=None,
                         which_fn=None, run_fn=None):
        """Spawn a fresh Coordinator session for one typed worker task."""
        if not self.supports_subagents:
            raise ValueError('SUBAGENT_UNSUPPORTED: %s' % self.id)
        argv = self.argv(model, which_fn=which_fn, run_fn=run_fn)
        return argv + self.coordinator_flags(task_type) if argv is not None else None

    # -- output face ---------------------------------------------------------
    def resolve_session(self, repo_cwd, exec_id, created_at):
        """Bind an execution to this CLI's session output on this machine:
        ``{'bound': 'exact'|'window'|None, 'path': handle|None}`` — exact wins
        by content (the prompt embeds the execution id), window only when
        exactly one recent candidate matches. The handle feeds ``tail``; it
        is opaque to every consumer. Default: this plugin produces no
        session output, and the run view degrades to the deterministic
        timeline instead of showing someone else's session."""
        return unbound()

    def tail(self, handle, offset=0, limit=None):
        """Incrementally parse the bound session output from the resume
        cursor ``offset`` (opaque: a byte offset for file-backed readers, an
        event count for storage-backed ones). Returns ``{'events', 'offset',
        'size', 'skipped'}`` in the shared flat-event contract; ``size`` is
        the session's total content in the same units as the cursor (so the
        client's exhaustion test ``offset >= size`` holds), and ``bytes`` —
        when present — carries the byte volume for the client's readout.
        Default mirrors ``resolve_session``: nothing to read, never a raise."""
        return {'events': [], 'offset': max(0, int(offset or 0)), 'size': 0,
                'skipped': 0}
