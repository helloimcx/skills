"""Spawned-session watching for the worker agent (split from agent.py).

The liveness half of ``WorkerAgent``: the startup grace window that turns a
spawn that cannot start into an immediate FAILED ack, and the death watcher
that turns a silently-dead session (issue #229) into a timely, visible failure
instead of a 4h phantom execution. Split out of agent.py 2026-09-23 purely to
keep agent.py under the 800-line red line; no behavior change — the methods
remain ``WorkerAgent`` members via ``SpawnWatchMixin`` and reach their
collaborators (``git_fn``/``popen_fn``/``spawn_grace_*``/``clock``/``_ack``)
through ``self`` exactly as before.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import review_round, trigger

# A spawned CLI that cannot start dies on its own, silently: the `cmd /c`
# batch death this branch removes took 4.48s and wrote zero bytes (measured
# 2026-09-18), and a healthy opencode round can take 14s to write its first
# byte — so neither speed nor output identifies a dead process, only its exit
# code does. The window is sized to catch the known failure with margin; the
# cost is that the SPAWNED ack (and with it the card's move to 审核中) waits
# this long. Watching it turns a phantom "running" round into a precise,
# persistent failure: the command is still CLAIMED, so the FAILED ack is the
# truthful outcome and the claim goes back with it.
SPAWN_GRACE_SECONDS = 8.0
SPAWN_GRACE_POLL_SECONDS = 0.25
# A spawned session that dies silently after the grace window (issue #229:
# opencode rounds vanished 18-44min in with no publish, no WER, no stderr)
# would hold its execution RUNNING until the 4h lease expires. The liveness
# watcher probes the pid periodically and, once it is gone, waits a confirm
# window for the round to finish normally (publish → sweep DONE) before
# failing the command and handing the item back.
PROCESS_DEATH_POLL_SECONDS = 30.0
PROCESS_DEATH_CONFIRM_SECONDS = 120.0


def _pid_alive(pid):
    """Best-effort liveness probe for the spawned session's process."""
    if pid is None or pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


class SpawnWatchMixin:
    def _start_liveness_watch(self, project, command_id, claim, info):
        """Liveness watch (issue #229): a session that dies silently after the
        grace window must not hold the execution until lease expiry. Best
        effort like progress reporting — a watcher that cannot start never
        fails the command."""
        if not info.get('pid'):
            return
        try:
            threading.Thread(
                target=self._watch_process,
                args=(project, command_id, claim, info['pid'],
                      info.get('proc'), info.get('log')),
                daemon=True).start()
        except RuntimeError as exc:
            print(json.dumps({'ok': False, 'code': 'LIVENESS_THREAD_FAILED',
                              'execution': claim['execution'],
                              'message': str(exc)}), flush=True)

    def _cleanup_prepared_worktree(self, claim, cfg):
        """Remove the worktree a failed round had already cut before its spawn
        could start; a no-op when the failure happened before the claim."""
        if claim is not None and cfg is not None:
            review_round.remove_worktree_quietly(
                cfg, claim.get('worktree'), self.git_fn)

    def _spawn_and_watch(self, cfg, row, spawn_command, state_dir, prompt, claim):
        """Spawn the round's session into its own per-round log and watch the
        startup window; returns ``(info, log_path)`` — the log path is None
        when the execution id could not key one. A process that cannot start is
        refused here, while the command is still CLAIMED: the FAILED ack is
        then the truthful outcome and carries the claim back (see ``_ack``), so
        a round that never ran frees its item immediately instead of at lease
        expiry — including the worktree it had already cut."""
        log_path = trigger.spawn_log_path(state_dir, claim['execution'])
        info = trigger.spawn_worker(
            {'spawn': {'command': spawn_command or row['spawn_command']},
             'repo_cwd': row['repo_cwd']},
            str(state_dir), prompt, False, popen_fn=self.popen_fn,
            log_path=log_path,
            # the round's own execution id names its prompt file: two
            # rounds on this machine must never share the stdin source
            prompt_key=claim['execution'])
        died = self._died_within_grace(info.get('proc'), self.spawn_grace_seconds)
        if died is not None:
            review_round.remove_worktree_quietly(cfg, claim.get('worktree'),
                                                 self.git_fn)
            raise review_round.PrepareError(
                500, 'SPAWN_EXIT: rc=%s（进程在启动窗口内退出，日志 %s）'
                     % (died, info.get('log')))
        return info, log_path

    def _log_dispatch(self, state_dir, number, claim, info):
        """Best effort: a round that started is never failed — let alone handed
        back — because its diagnostic line could not be written."""
        try:
            trigger.log_decision(str(state_dir), self.clock(),
                                 {'ok': True, 'event': 'COMMAND_DISPATCH',
                                  'matched_numbers': [number],
                                  'execution': claim['execution'],
                                  'pid': info['pid']})
        except Exception as exc:
            print(json.dumps({'ok': False, 'code': 'DISPATCH_LOG_FAILED',
                              'execution': claim['execution'],
                              'message': '%s: %s' % (type(exc).__name__, exc)}),
                  flush=True)

    def _died_within_grace(self, proc, grace):
        """The non-zero exit code seen inside the startup window, or None when
        the process is still running — or exited cleanly: a zero exit means it
        ran and finished, which is the round's business (publish, sweep), not a
        failure to start. A process object without a callable ``poll`` is
        unobservable, never judged dead on a guess."""
        poll = getattr(proc, 'poll', None)
        if not grace or not callable(poll):
            return None
        step = self.spawn_grace_poll_seconds
        for _ in range(max(1, int(grace / step))):
            code = poll()
            if isinstance(code, int) and not isinstance(code, bool):
                return code if code != 0 else None
            time.sleep(step)
        return None

    def _watch_process(self, project, command_id, claim, pid, proc=None,
                       log_path=None):
        """Probe the spawned session's pid until it is gone, then fail the
        command and hand the item back — unless the round finished normally in
        the confirm window (its verdict published, the command swept to DONE,
        so the FAILED ack is a CAS no-op). Turns a silent death (issue #229)
        into a timely, visible failure instead of a 4h phantom execution. The
        ack names the exit code and quotes the round log's tail: a dead
        session gets one diagnosis, not a dead end (03d8c5c7 postmortem)."""
        while _pid_alive(pid):
            time.sleep(PROCESS_DEATH_POLL_SECONDS)
        time.sleep(PROCESS_DEATH_CONFIRM_SECONDS)
        self._ack(project, command_id, 'FAILED',
                  fail_reason=self._death_reason(proc, log_path),
                  claim=claim)

    def _death_reason(self, proc, log_path):
        """`PROCESS_DIED: rc=<code|unknown>; log tail: …` — the exit code when
        the process object is observable, else the last non-blank lines of the
        round's own CLI log (the only record a silent death leaves). Read
        failures degrade to the bare head; the whole reason stays ≤800 chars."""
        poll = getattr(proc, 'poll', None)
        rc = 'unknown'
        if callable(poll):
            try:
                code = poll()
            except Exception:   # a raising poll must not kill the watcher
                code = None     # thread before the FAILED ack goes out
            if isinstance(code, int) and not isinstance(code, bool):
                rc = str(code)
        reason = 'PROCESS_DIED: rc=%s' % rc
        if not log_path:
            return reason
        try:
            lines = [line.strip() for line in
                     Path(log_path).read_text(encoding='utf-8',
                                              errors='replace').splitlines()
                     if line.strip()][-6:]
        except OSError:
            return reason
        if lines:
            reason += '; log tail: ' + ' | '.join(line[:200] for line in lines)
        return reason[:800]
