"""Operator round execution: the agent-side operator_round branch.

Deliberately self-contained on the serve host: claim from the machine-level
operator store, build the prompt (rules doc + signal identity + CLI contract,
never the payload — 门铃不是信封), spawn headless detached, ack SPAWNED, and
watch the process (death → FAILED verdict, #229 semantics scoped locally).
No worktree, no bind, no project coordinator.
"""
import json
import socket
import threading
import time
from pathlib import Path

from .. import trigger
from . import face, store as op_store

OPERATOR_TTL = 1800
DEATH_POLL_SECONDS = 30.0
DEATH_CONFIRM_SECONDS = 120.0
WORKER_SUFFIX = '/operator'


def rules_doc_path():
    """The in-repo operator rules doc (walk-up from this file, junction-safe
    enough at fixed parents). Missing is fail-closed: a round that
    cannot start must not cost a claim."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        for rel in (
            Path('references') / 'operator.md',
            Path('services') / 'code-loop' / 'references' / 'operator.md',
            Path('skills') / 'code-loop' / 'references' / 'operator.md',
        ):
            candidate = parent / rel
            if candidate.is_file():
                return candidate
    raise ValueError('OPERATOR_RULES_MISSING: references/operator.md')


def build_prompt(claim, item, rules_doc):
    """Bootstrap + directed suffix. Only identifiers and where-to-verify paths
    ride along; the signal payload stays in the store (the session reads it)."""
    execution = claim['id']
    token = claim['token']
    cli = cli_path()
    return (
        '你是 code-loop 的 operator 轮（CP 协调监控，L1 白名单自治）。\n'
        '先完整读规则文档：%s\n'
        '\n'
        '本轮信号（只含标识符，事实在机器本机库，自行查证）：\n'
        '- item key: %s\n'
        '- execution: %s  token: %d\n'
        '- state 目录: %s\n'
        '\n'
        '查证与动作只经稳定 CLI（绝对路径；你的 cwd 是 state 目录）：\n'
        '  python "%s" status\n'
        '  python "%s" act --execution %s --token %d --verb <verb> '
        '[--target T --detail D]\n'
        '  python "%s" report --execution %s --token %d --file <report.json>\n'
        '\n'
        'report.json 契约（缺一即拒）：{"signal": "%s", "diagnosis": str, '
        '"actions": [{"verb": ..., "target": ..., "detail": ...}], '
        '"outcome": RESOLVED|MITIGATED_WATCHING|ESCALATED_HUMAN|FALSE_POSITIVE_NOOP, '
        '"evidence": [...]}\n'
        '动作必须是 act 执行过的白名单动词（no_op 除外）；危险动作一律 '
        'outcome=ESCALATED_HUMAN 并留证，绝不自行执行。\n'
        % (rules_doc, item['key'], execution, token, item_state_dir(item),
           cli, cli, execution, token, cli, execution, token, item['key']))


def item_state_dir(item):
    return str(op_store.state_dir())


def _default_spawn_argv():
    """DEFAULT_COMMAND resolved past the shim: bare names land on npm .CMD
    shims, and `cmd.exe /c <shim>` + DETACHED_PROCESS dies rc=1 with zero
    output. Resolve, then swap the shim for its native exe target when one
    can be read (no resolvable target → return the resolution as-is and let
    spawn fail loudly downstream, never invent)."""
    from ..agent_runners.opencode import native_exe_for
    from ..cli import resolve_command
    resolved = resolve_command(list(trigger.DEFAULT_COMMAND))
    if len(resolved) >= 3 and resolved[0].lower() in ('cmd.exe', 'cmd') \
            and resolved[1] == '/c':
        native = native_exe_for(resolved[2])
        if native:
            return [native] + resolved[3:]
    return resolved


def cli_path():
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / 'services' / 'code-loop' / 'scripts' / 'operator.py'
        if candidate.is_file():
            return str(candidate)
    return 'services/code-loop/scripts/operator.py'


def execute_operator_round(agent, command):
    """Entry from agent.execute: run one operator_round command to SPAWNED
    (or a precise FAILED ack), then hand the live round to the watcher."""
    store = op_store.open_store()
    machine = socket.gethostname()
    command_id = command.get('id')
    claim = None
    try:
        item_id = command.get('item')
        if not item_id:
            raise ValueError('INVALID_COMMAND: operator_round needs item')
        claim = face.claim(store, worker=machine + WORKER_SUFFIX,
                           ttl=OPERATOR_TTL, item_id=item_id)
        item = store.db.execute('SELECT * FROM work_items WHERE id=?',
                                (claim['item_id'],)).fetchone()
        rules = rules_doc_path()
        prompt = build_prompt(claim, dict(item), rules)
        agent_name = command.get('agent')
        # None argv is a real plugin contract ("use this machine's registered
        # spawn command" — claude preset-returns None). Operator rounds have
        # no registration to fall back on, so the default command is the
        # fallback — resolved through the native-exe discipline: a bare name
        # lands on the npm .CMD shim and `cmd.exe /c <shim>` + DETACHED
        # exits 1 with zero output (the opencode silent-death lesson; every
        # working loop registration binds the native exe — production
        # catch 2026-10-08: both enabled-mode rounds died exactly there).
        argv = (agent.spawn_argv_fn(agent_name, command.get('model'))
                if agent_name else None) or _default_spawn_argv()
        log_path = store.state_dir / 'executions' / ('%s.log' % claim['id'])
        cfg = {'spawn': {'command': argv}, 'repo_cwd': str(store.state_dir)}
        info = trigger.spawn_worker(cfg, store.state_dir / 'executions', prompt,
                                    dry_run=False,
                                    prompt_key='operator-%s' % claim['id'],
                                    log_path=str(log_path))
        face.ack_spawned(store, command_id, machine, claim['id'])
        threading.Thread(target=_watch, args=(store, machine, command_id, claim, info),
                         daemon=True).start()
    except Exception as exc:
        try:
            face.ack_failed(store, command_id, machine,
                            '%s: %s' % (type(exc).__name__, exc), claim=claim)
        except Exception:  # noqa: BLE001 - the FAILED ack is best-effort on an
            # already-failing path; a second fault must not mask the original
            # exception escaping execute_operator_round (review MAJOR-2).
            pass
    finally:
        store.db.close()


def _death_reason(proc):
    """One watch pass: a death reason string, None while alive. A poll hit is
    confirmed after the window before it counts (#229 semantics)."""
    try:
        if proc.poll() is None:
            return None
    except (OSError, ValueError):
        return 'PROCESS_DIED: poll failed'
    time.sleep(DEATH_CONFIRM_SECONDS)
    try:
        if proc.poll() is None:
            return None   # came back within the confirm window: not a death
        return 'PROCESS_DIED: exit=%s' % proc.poll()
    except (OSError, ValueError):
        return 'PROCESS_DIED: poll failed after confirm'


def _watch(store, machine, command_id, claim, info):
    """Process-death watch scoped to the operator store (#229 semantics): a
    confirmed death becomes a FAILED verdict on the round and a FAILED ack on
    the command — the ledger shows a dead round, not a live one."""
    proc = info.get('proc')
    if proc is None:
        return
    deadline = time.time() + OPERATOR_TTL
    reason = None
    while time.time() < deadline:
        reason = _death_reason(proc)
        if reason is not None:
            break
        time.sleep(DEATH_POLL_SECONDS)
    if reason is None:
        return
    live = op_store.open_store()
    try:
        face.fail_round(live, claim['id'], claim['token'], reason)
        face.ack_failed(live, command_id, machine, reason, claim=claim)
    finally:
        live.db.close()
