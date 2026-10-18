"""Resident worker agent: the machine-side half of the command channel.

One long-lived WebSocket to the CP server; a pushed command is executed
entirely on this machine — registration facts, worktree, bind and the spawned
session are all local, and the command payload itself carries no executable
content (doorbell, not envelope; the prompt template is code). Push is an
accelerator, the ledger is the floor: a dropped connection just means the
command waits on the ledger until this agent reconnects, and the TTL sweep
makes a never-served wait visible instead of lossy. The cron loops stay the
correctness floor — nothing here schedules them.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import queue
import random
import threading
import time

from . import agent_runners, review_round, trigger
from .operator import round as operator_round
from .cp import ws as ws_proto
from .cp.client import Client, ClientError
from .cp.commands import validated_spawn_options
from .cp.ws import FrameReader
from .cp.runtime import load_global_worker_config, load_worker_server
from .cp.worker import WorkerService
from .agent_watch import (SpawnWatchMixin, SPAWN_GRACE_SECONDS,
                          SPAWN_GRACE_POLL_SECONDS, _pid_alive)
from .dash import collect, store as dash_store

MAX_BACKOFF_SECONDS = 30.0
_CLOSED = object()   # queue sentinel: the frame reader is gone
_registration_fields = ('repo_cwd', 'entry_doc', 'rules_doc', 'spawn_command')

# Which rounds stream their own CLI log instead of a claude transcript: an
# opencode round keeps no transcript to bind, so its log IS the stream.
STREAM_LOG_AGENTS = ('opencode',)

# Refresh cadence: the registry's own detection cache TTL.
CAPABILITY_REFRESH_SECONDS = int(agent_runners.CACHE_TTL_SECONDS)


class _CapabilityFrame:
    """A ready ``agent_options`` frame handed from the detection pump to the
    channel loop — the wrapper keeps it unambiguous beside command dicts."""

    __slots__ = ('frame',)

    def __init__(self, frame):
        self.frame = frame


def capability_frame(options_fn=None):
    """The agent_options payload: the registry's own detection reduced to
    ``{id, models}``. label/sub never leave the machine — the server composes
    dialog cards from its own registry (doorbell, not envelope)."""
    options = (options_fn or agent_runners.options)()
    return {'type': 'agent_options',
            'agents': [{'id': row.get('id'), 'models': row.get('models')}
                       for row in options.get('agents', [])]}


def backoff_seconds(attempt):
    """Exponential with jitter, capped — reconnect storms stay polite."""
    return min(MAX_BACKOFF_SECONDS, 2.0 ** min(attempt, 5) * (0.5 + random.random()))


def _numbered(fresh, seq):
    """Attach 1-based seqs to one tail window's events."""
    batch = []
    for event in fresh:
        seq += 1
        batch.append({'seq': seq, **event})
    return batch, seq


# The command actions this worker executes, and the round each one carries:
# the loop names the claim queue and item prefix (review:<n> / repair:<n>),
# the prompt builder the directive the adopted session follows. A registered
# action with an extra pre-claim requirement validates it on the registration
# row before anything is claimed or cut — a round that cannot start must not
# cost a lease or a worktree.
ROUND_LOOPS = {'review_round': 'reviewer', 'repair_round': 'handler'}
# The prompt is loop-generic: trigger.build_round_prompt resolves each loop's
# own rule/entry docs. The registration's docs belong to the reviewer loop —
# a handler/discover round inheriting them reads the wrong loop's rules
# (2026-09-24: a repair session opened by reading reviewer.md and looked
# like a reviewer round).


def _no_requirement(row):
    return None


_PRECLAIM_CHECKS = {'repair_round': lambda row: trigger.loop_doc_path(
    row['rules_doc'], ROUND_LOOPS['repair_round'])}


def round_stream_path(agent_name, log_path):
    """The stream the progress follower should tail for this round: a round
    driven by a CLI with no claude transcript streams its own log; a claude
    round keeps the transcript binding untouched (richer than its stdout).
    Without a per-round log path there is nothing to adopt — the shared cron
    log is never read as this round's stream."""
    if agent_name in STREAM_LOG_AGENTS and log_path:
        return str(log_path)
    return None


class WorkerAgent(SpawnWatchMixin):
    def __init__(self, server_url, token=None, db_path=None, projects_root=None,
                 popen_fn=None, git_fn=None, client=None, registration_loader=None,
                 cfg_factory=None, worker_factory=None, clock=time.time,
                 follow_fn=None,
                 spawn_argv_fn=None,
                 spawn_grace_seconds=SPAWN_GRACE_SECONDS,
                 spawn_grace_poll_seconds=SPAWN_GRACE_POLL_SECONDS,
                 capability_refresh_seconds=CAPABILITY_REFRESH_SECONDS,
                 capability_options_fn=None):
        self.client = client or Client(
            {'url': server_url, 'token_env': 'CODE_LOOP_CP_TOKEN'}, token=token)
        self.db_path = db_path or str(collect.DEFAULT_DB)
        self.projects_root = projects_root or str(collect.DEFAULT_ROOT)
        self.popen_fn = popen_fn
        self.git_fn = git_fn
        self.registration_loader = registration_loader or self._load_registration
        self.cfg_factory = cfg_factory or self._load_cfg
        self.worker_factory = worker_factory or (lambda cfg: WorkerService(cfg))
        self.clock = clock
        self.follow_fn = follow_fn if follow_fn is not None else self._default_follow
        self.spawn_argv_fn = spawn_argv_fn or self._spawn_argv
        self.spawn_grace_seconds = spawn_grace_seconds
        self.spawn_grace_poll_seconds = spawn_grace_poll_seconds
        self.capability_refresh_seconds = capability_refresh_seconds
        self.capability_options_fn = capability_options_fn
        self._capability = None   # last-known snapshot; reconnecting hellos announce it

    # -- machine-local facts -------------------------------------------------
    def _load_registration(self, project):
        """The registered execution facts for one project, from this machine's
        dashboard store (dashboard.py register writes them)."""
        db = dash_store.connect(self.db_path)
        try:
            row = db.execute(
                'SELECT config_path, repo_cwd, entry_doc, rules_doc, spawn_command '
                'FROM projects WHERE org_repo=?', (project,)).fetchone()
        finally:
            db.close()
        return dict(row) if row else None

    def _load_cfg(self, project, row):
        """The machine-global worker config, resolved against the registered
        repo (the project key comes from that checkout's git origin, or the
        documented ``server.project`` pin). One file per machine: the CP
        server pointer plus this machine's execution facts — identical to what
        the worker CLI resolves for a claim, so the agent and the sessions it
        spawns agree on state_dir/worktree_root by construction."""
        try:
            return load_global_worker_config(cwd=row['repo_cwd'])
        except Exception as exc:
            raise review_round.PrepareError(
                403, 'REGISTER_INVALID: worker config: %s: %s'
                % (type(exc).__name__, exc)) from None

    def _checked_registration(self, project):
        row = self.registration_loader(project)
        if row is None:
            raise review_round.PrepareError(
                403, 'NOT_REGISTERED: %s（dashboard.py register --project %s）'
                % (project, project))
        missing = [name for name in _registration_fields if not row.get(name)]
        if missing:
            raise review_round.PrepareError(
                403, 'NOT_REGISTERED: %s（dashboard.py register）' % ','.join(missing))
        for name, kind in (('repo_cwd', 'dir'), ('entry_doc', 'file'),
                           ('rules_doc', 'file')):
            p = Path(row[name])
            if not (p.is_dir() if kind == 'dir' else p.is_file()):
                raise review_round.PrepareError(
                    403, 'REGISTER_INVALID: %s: %s' % (name, p))
        try:
            row['spawn_command'] = json.loads(row['spawn_command'])
        except ValueError as exc:
            raise review_round.PrepareError(
                403, 'REGISTER_INVALID: spawn_command: %s' % exc) from None
        return row

    # -- spawn-option presets -------------------------------------------------
    @staticmethod
    def _spawn_argv(agent, model):
        """argv for the requested agent plugin, or None = this machine's
        registered spawn command. Resolution happens here, on the machine that
        pays for the session: a plugin re-checks a requested model against
        this machine's own list before any claim is taken."""
        return agent_runners.spawn_argv(agent, model)

    # -- command execution ----------------------------------------------------
    def execute(self, command):
        """Run one pushed command on this machine and ack the outcome. Every
        refusal is precise and ledger-visible; a command that starts never
        blocks the frame loop (execution runs on the worker thread)."""
        project = command.get('project') or ''
        number = command.get('number')
        command_id = command.get('id')
        claim = None
        cfg = None
        try:
            action = command.get('action')
            if action == 'operator_round':
                # The operator branch is deliberately self-contained (machine
                # level store, no project coordinator/worktree/bind) — see
                # operator/round.py. Nothing below applies to it.
                operator_round.execute_operator_round(self, command)
                return
            if action not in ROUND_LOOPS:
                raise review_round.PrepareError(
                    400, 'INVALID_COMMAND: unknown action %r' % action)
            if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
                raise review_round.PrepareError(
                    400, 'INVALID_COMMAND: number must be a positive integer')
            # Spawn options are validated a second time on the executor and
            # resolved BEFORE the claim: an impossible request (unknown agent,
            # model missing from this machine) must not cost a lease, a
            # worktree, or the claim's 300s hold on the item.
            agent_name, model = validated_spawn_options(
                command.get('agent'), command.get('model'))
            spawn_command = self.spawn_argv_fn(agent_name, model)
            row = self._checked_registration(project)
            _PRECLAIM_CHECKS.get(action, _no_requirement)(row)
            cfg = self.cfg_factory(project, row)
            coordinator = review_round.RemoteCoordinator(self.worker_factory(cfg),
                                                         loop=ROUND_LOOPS[action])
            claim = review_round.prepare_round(
                coordinator, cfg, number, git_fn=self.git_fn)
            prompt = trigger.build_round_prompt(ROUND_LOOPS[action], row,
                                                number, claim)
            # Diagnostics land where this machine keeps its round state: the
            # registered config's sibling state dir when one is known (the
            # dashboard host), else the worker config's own state dir.
            cfg_dir = (Path(row['config_path']).parent if row.get('config_path')
                       else Path(cfg['state_dir']))
            state_dir = cfg_dir / 'state'
            # Nothing may follow the spawn inside this block: every handler
            # below hands the claim back, which is only correct while no
            # session of this round is alive. Post-spawn bookkeeping belongs
            # in the else branch.
            info, log_path = self._spawn_and_watch(cfg, row, spawn_command,
                                                   state_dir, prompt, claim)
        except review_round.PrepareError as exc:
            self._ack(project, command_id, 'FAILED', fail_reason=exc.message,
                      claim=claim)
        except ValueError as exc:
            # spawn-option refusals (AGENT_MISSING / MODEL_UNAVAILABLE) arrive
            # code-shaped from the agent-runner plugin; terminal by design —
            # the click chose this model for this machine, requeuing would not
            # help. The repair round can also raise here after the claim (the
            # handler doc vanishing between the pre-claim check and the prompt
            # build): the cleanup is a no-op while nothing was prepared.
            self._cleanup_prepared_worktree(claim, cfg)
            self._ack(project, command_id, 'FAILED', fail_reason=str(exc),
                      claim=claim)
        except OSError as exc:
            # A round that could not start (e.g. the registered binary is
            # missing) would leave its worktree behind — prepare_round's own
            # cleanup only covers its internal failures, not a failure after
            # the worktree was cut.
            self._cleanup_prepared_worktree(claim, cfg)
            self._ack(project, command_id, 'FAILED',
                      fail_reason='SPAWN_FAILED: %s' % exc, claim=claim)
        except Exception as exc:
            self._cleanup_prepared_worktree(claim, cfg)
            self._ack(project, command_id, 'FAILED',
                      fail_reason='AGENT_ERROR: %s: %s' % (type(exc).__name__, exc),
                      claim=claim)
        else:
            # The round is live: the item and its worktree are the session's
            # now, and nothing here may fail the command or hand it back.
            self._log_dispatch(state_dir, number, claim, info)
            # The execution id is what lets the ledger close this command when
            # the round ends (store.sweep_commands derives DONE from it).
            self._ack(project, command_id, 'SPAWNED',
                      execution=claim['execution'])
            # Progress reporting is best effort and never round-affecting: a
            # daemon thread tails the round's stream and pushes batches to the
            # ledger so a remote round reads like a local one.
            if self.follow_fn is not None:
                try:
                    threading.Thread(
                        target=self.follow_fn,
                        args=(project, claim['execution'], row['repo_cwd'],
                              info.get('pid'), self.clock(), agent_name),
                        kwargs={'stream_path': round_stream_path(
                            agent_name, log_path)},
                        daemon=True).start()
                except RuntimeError as exc:   # e.g. thread resources exhausted
                    print(json.dumps({'ok': False, 'code': 'PROGRESS_THREAD_FAILED',
                                      'execution': claim['execution'],
                                      'message': str(exc)}), flush=True)
            self._start_liveness_watch(project, command_id, claim, info)

    def _ack(self, project, command_id, outcome, fail_reason=None, execution=None,
             claim=None):
        """A failed command carries its claim (execution + token) when one was
        taken: the server releases the round with it, so a spawn that died
        cannot leave a phantom execution holding the item for hours."""
        body = {'command': command_id, 'outcome': outcome,
                'fail_reason': fail_reason, 'execution': execution}
        if claim is not None:
            body['execution'] = claim['execution']
            body['token'] = claim['token']
        try:
            self.client.call('POST', '/api/projects/%s/command_ack' % project, body)
        except Exception as exc:
            print(json.dumps({'ok': False, 'code': 'COMMAND_ACK_FAILED',
                              'message': '%s: %s' % (type(exc).__name__, exc)}),
                  flush=True)

    # -- session progress (best-effort stream behind a spawned round) ----------
    # The spawned session writes its output on this machine; the control
    # plane only sees the round's facts. The follower bridges the gap: it
    # binds the session through the requested agent's plugin (the same
    # binding the run-detail view uses), tails it in small batches and
    # pushes them to the project ledger, where the dashboard's run view reads
    # them back verbatim — a remote round then streams exactly like a local
    # one. Every failure is swallowed after a few retries: the review round
    # itself never depends on this thread.

    PROGRESS_POLL_SECONDS = 2.0
    PROGRESS_IDLE_LIMIT_SECONDS = 120.0
    PROGRESS_MAX_REPORT_FAILURES = 5
    PROGRESS_BATCH_EVENTS = 100   # approved batch cap; a bigger tail
                                  # window is split into several POSTs

    def hello_frame(self):
        """The connect-time announcement: which projects this machine holds
        execution registrations for (a repo_cwd on file). Empty — never a
        missing frame or key — when the dashboard store is unreadable."""
        projects = []
        try:
            db = dash_store.connect(self.db_path)
            try:
                projects = [row['org_repo'] for row in db.execute(
                    'SELECT org_repo FROM projects '
                    "WHERE repo_cwd IS NOT NULL AND repo_cwd != ''")]
            finally:
                db.close()
        except Exception:
            projects = []
        import socket
        frame = {'type': 'hello', 'machine': socket.gethostname(),
                 'registrations': sorted(projects)}
        if self._capability is not None:
            # The last-known snapshot rides the hello: a reconnecting agent
            # projects immediately. A first-ever connect has none — the
            # capability pump follows up with an agent_options frame.
            frame['agents'] = self._capability['agents']
        return frame

    def report_progress(self, project, execution, events, done=False):
        return self.client.call(
            'POST', '/api/projects/%s/command_progress' % project,
            {'execution': execution, 'events': events, 'done': done})

    def _stream_to_tail(self, stream_path, repo_cwd, execution, started_at,
                        clock, poll, runner):
        """The stream this round reports from: named outright when the round's
        CLI keeps no claude transcript, otherwise bound by content as before."""
        if stream_path:
            return stream_path
        return self._bind_progress_path(repo_cwd, execution, started_at,
                                        clock, poll, runner)

    def _bind_progress_path(self, repo_cwd, execution, started_at, clock, poll,
                            runner):
        """Poll for the session binding until it appears or the plugin's
        binding window closes; ``None`` means the round streams nothing."""
        deadline = started_at + (agent_runners.base.BIND_WINDOW_AFTER
                                 + agent_runners.base.BIND_SLOP_BEFORE)
        while clock() <= deadline:
            binding = runner.resolve_session(repo_cwd, execution, started_at)
            if binding['path']:
                return binding['path']
            time.sleep(poll)
        return None

    def _report_window(self, project, execution, batch, failures):
        """Split one tail window into approved-size posts: an oversized
        single POST can trip the client's request-body limit and the whole
        window would be lost (the offset has already moved forward).
        Returns the failure streak after the last post."""
        for start in range(0, len(batch), self.PROGRESS_BATCH_EVENTS):
            failures = self._report_batch(
                project, execution,
                batch[start:start + self.PROGRESS_BATCH_EVENTS], failures)
            if failures >= self.PROGRESS_MAX_REPORT_FAILURES:
                break
        return failures

    def _report_batch(self, project, execution, batch, failures):
        """One reported batch; returns the failure streak after the attempt
        (reset on success) — the caller gives up once it caps out."""
        try:
            self.report_progress(project, execution, batch)
            return 0
        except Exception as exc:
            print(json.dumps(
                {'ok': False, 'code': 'PROGRESS_REPORT_FAILED',
                 'execution': execution,
                 'message': '%s: %s' % (type(exc).__name__, exc)}),
                flush=True)
            return failures + 1

    def follow_progress(self, project, execution, repo_cwd, pid, started_at,
                        agent=None, runner=None, pid_alive=None, poll=None,
                        idle_limit=None, clock=None, stream_path=None):
        """Tail the session output the requested agent's plugin binds, until
        the session process exits or the stream goes quiet past
        ``idle_limit``, reporting each new batch with 1-based seqs and a
        final ``done`` marker. Gives up silently-in-log when nothing binds
        within the plugin's binding window.

        ``stream_path`` names the stream outright (a round whose CLI keeps no
        claude transcript writes its own log): the binding wait is skipped
        entirely, so such a round streams from its first second instead of
        waiting out a window that can never be satisfied. Without it the
        transcript is bound as before, and a round with no transcript binds
        nothing (gives up silently-in-log)."""
        clock = clock or self.clock
        poll = self.PROGRESS_POLL_SECONDS if poll is None else poll
        idle_limit = (self.PROGRESS_IDLE_LIMIT_SECONDS if idle_limit is None
                      else idle_limit)
        pid_alive = pid_alive or _pid_alive
        if stream_path:
            # A named stream is the round's own log file: read it with the
            # shared line parser (both CLI dialects), never the plugin's
            # storage reader — its handle is a session id, not a path. Same
            # choice runapi's log session makes.
            runner = agent_runners.claude_transcript
        else:
            runner = runner or agent_runners.session_reader(agent)
        path = self._stream_to_tail(stream_path, repo_cwd, execution,
                                    started_at, clock, poll, runner)
        if path is None:
            return
        self._tail_and_report(project, execution, runner, path, pid, pid_alive,
                              poll, idle_limit, clock)

    def _tail_and_report(self, project, execution, runner, path, pid,
                         pid_alive, poll, idle_limit, clock):
        """The tail loop proper: numbered batches out, ``done`` marker last."""
        seq = 0
        offset = 0
        idle = 0.0
        failures = 0
        while True:
            result = runner.tail(path, offset)
            fresh, offset = result['events'], result['offset']
            if fresh:
                idle = 0.0
                batch, seq = _numbered(fresh, seq)
                failures = self._report_window(project, execution, batch,
                                               failures)
                if failures >= self.PROGRESS_MAX_REPORT_FAILURES:
                    return
            if (pid is None or pid_alive(pid)) and idle < idle_limit:
                if not fresh:
                    idle += poll
                    time.sleep(poll)
                continue
            break
        try:
            self.report_progress(project, execution, [], done=True)
        except Exception:
            pass

    def _default_follow(self, project, execution, repo_cwd, pid, started_at,
                        agent=None, stream_path=None):
        try:
            self.follow_progress(project, execution, repo_cwd, pid, started_at,
                                 agent=agent, stream_path=stream_path)
        except Exception as exc:   # never let reporting take the agent down
            print(json.dumps({'ok': False, 'code': 'PROGRESS_FOLLOW_FAILED',
                              'execution': execution,
                              'message': '%s: %s' % (type(exc).__name__, exc)}),
                  flush=True)

    # -- the channel -----------------------------------------------------------
    def run(self, stop=None):
        """Connect, serve frames, reconnect with backoff. A dead channel never
        loses a command: it stays on the ledger until this agent is back."""
        attempt = 0
        while stop is None or not stop.is_set():
            try:
                self._connect_and_serve(stop)
                attempt = 0
            except Exception as exc:
                print(json.dumps({'ok': False, 'code': 'AGENT_DISCONNECTED',
                                  'message': '%s: %s' % (type(exc).__name__, exc),
                                  'attempt': attempt + 1}, ensure_ascii=False),
                      flush=True)
            if stop is not None and stop.is_set():
                break
            time.sleep(backoff_seconds(attempt))
            attempt += 1

    def _connect_and_serve(self, stop=None):
        """One connection lifetime: upgrade, then serve frames until either
        side closes. Returns (or raises) when the channel is gone — the read
        thread can die from a dead socket, so the main loop must never depend
        on the stop flag alone to notice (a silent reader would leave the
        agent looking connected forever)."""
        sock = self.client.raw_socket()
        client_key = base64.b64encode(os.urandom(16)).decode('ascii')
        request = ('GET /api/v1/ws HTTP/1.1\r\n'
                   'Host: %s\r\n'
                   'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                   'Sec-WebSocket-Key: %s\r\n'
                   'Sec-WebSocket-Version: 13\r\n'
                   'X-CP-Protocol: 1\r\n'
                   'Authorization: Bearer %s\r\n\r\n'
                   % (self.client.host, client_key, self.client.token))
        sock.sendall(request.encode('ascii'))
        head, rest = self._read_head(sock)
        status = head.split(b'\r\n', 1)[0].split()
        if len(status) < 2 or status[1] != b'101':
            sock.close()
            raise ConnectionError('upgrade refused: %s' %
                                  head.split(b'\r\n', 1)[0].decode('ascii', 'replace'))
        self._verify_accept(head, client_key, sock)
        try:
            sock.sendall(ws_proto.encode_client_text(
                json.dumps(self.hello_frame(), ensure_ascii=False)))
        except OSError:
            pass   # the read loop notices; the command catch-up still works
        commands = queue.Queue()
        caps_stop = threading.Event()
        caps = threading.Thread(target=self._capability_pump,
                                args=(commands, caps_stop), daemon=True)
        caps.start()
        # Any frame the server flushed right after the 101 came in with the
        # handshake read; the reader starts from it instead of the socket.
        reader = FrameReader(sock, rest)
        pump = threading.Thread(target=self._serve_frames, args=(reader, commands),
                                daemon=True)
        pump.start()
        try:
            while True:
                if stop is not None and stop.is_set():
                    return
                try:
                    command = commands.get(timeout=1.0)
                except queue.Empty:
                    if not pump.is_alive():
                        raise ConnectionError('frame reader stopped')
                    continue
                if command is _CLOSED:
                    return
                if isinstance(command, _CapabilityFrame):
                    # The send stays on this thread: the socket's only other
                    # writer is the reader's pong, and detection subprocesses
                    # must never touch the frame loop.
                    try:
                        sock.sendall(ws_proto.encode_client_text(
                            json.dumps(command.frame, ensure_ascii=False)))
                    except OSError:
                        pass   # the read loop notices; reconnect re-announces
                    continue
                self.execute(command)
        finally:
            caps_stop.set()
            try:
                sock.close()
            except OSError:
                pass

    def _capability_pump(self, commands, caps_stop):
        """Every ``capability_refresh_seconds``, hand the channel loop a ready
        agent_options frame. Detection runs HERE — a listing costs a CLI start,
        so a slow one must not delay command delivery. A failed detection is
        skipped (next round retries the registry cache); the pump must never die,
        or the projection ages out and the dialog falls back to the serve host."""
        while not caps_stop.is_set():
            try:
                frame = capability_frame(self.capability_options_fn)
                self._capability = frame
                commands.put(_CapabilityFrame(frame))
            except Exception:
                pass   # a dead pump is worse than a stale list
            caps_stop.wait(self.capability_refresh_seconds)

    def _verify_accept(self, head, client_key, sock):
        """RFC 6455 §4.1 REQUIRED check: the server must echo the accept key
        derived from ours — a mismatched or missing one is not our server."""
        accept = None
        for line in head.split(b'\r\n')[1:]:
            if line.lower().startswith(b'sec-websocket-accept:'):
                accept = line.split(b':', 1)[1].strip().decode('ascii', 'replace')
        if accept != ws_proto.accept_key(client_key):
            sock.close()
            raise ConnectionError('bad Sec-WebSocket-Accept from server')

    def _serve_frames(self, reader, commands):
        try:
            while True:
                opcode, payload = ws_proto.read_frame(reader, masked_required=False)
                if opcode == ws_proto.OP_CLOSE:
                    reader.source.close()
                    return
                if opcode == ws_proto.OP_PING:
                    reader.source.sendall(ws_proto.encode_client_frame(
                        ws_proto.OP_PONG, payload))
                    continue
                if opcode == ws_proto.OP_PONG:
                    continue
                if opcode == ws_proto.OP_TEXT:
                    message = json.loads(payload.decode('utf-8'))
                    if message.get('type') == 'command' and isinstance(
                            message.get('command'), dict):
                        commands.put(message['command'])
        except Exception:
            pass   # the main loop notices via _CLOSED and reconnects
        finally:
            commands.put(_CLOSED)

    def _read_head(self, sock):
        """Read the upgrade response head; returns (head, leftover_bytes) —
        the leftover belongs to the frame stream, never discarded."""
        sock.settimeout(10)
        buf = b''
        while b'\r\n\r\n' not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError('closed during upgrade')
            buf += chunk
        sock.settimeout(None)
        head, _, rest = buf.partition(b'\r\n\r\n')
        return head, rest


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(prog='worker_agent.py',
                                description='Resident worker agent: WS command channel')
    p.add_argument('--server', help='CP server URL (default: the machine-global '
                                    'worker config ~/.code-loop/control-plane.json)')
    p.add_argument('--db', help='dashboard store with this machine\'s registrations')
    args = p.parse_args(argv)
    try:
        # startup only needs the server pointer: a scheduled task's cwd is not
        # a git repo, so the project-key derivation of the full loader must
        # not run here (each command names its project and loads with the
        # registration's repo_cwd instead)
        url = args.server or load_worker_server()['url']
    except ValueError as exc:
        print(str(exc), file=__import__('sys').stderr)
        return 2
    agent = WorkerAgent(server_url=url, token=None, db_path=args.db)
    agent.run()
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
