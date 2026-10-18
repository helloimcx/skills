"""Command ledger surface: operator instructions to worker agents.

The consumer of a command is a deterministic agent, never an LLM session —
eligibility is adjudicated at dispatch (server: grants + enabled + a live
connection, see ``server.CommandDispatcher``); claimability is adjudicated at
the ordinary claim path the spawned session then walks. This mixin carries
the Service-facing verbs; the ledger state machine itself lives in
``Store.enqueue_command`` and friends.
"""
from ..agent_runners import validated as validated_spawn_options
from .store import Store

# Spawn options a click may attach to a review_round command: a whitelist of
# registered agent ids plus a strict model-id charset. The pair only ever
# picks an argv preset in executor code — it never reaches a prompt (doorbell,
# not envelope), but it is still operator input, so it is validated at the
# single enqueue surface, fail-closed. Both rules live with the plugins they
# describe (``codeloop.agent_runners``) so the whitelist cannot drift from the
# presets that answer it; this module only re-exports the entry point.


class CommandLedgerMixin:
    """Service methods for the command ledger (mixed into ``Service``)."""

    def enqueue_command(self, action, number, agent=None, model=None, now=None):
        if action not in Store.COMMAND_ACTIONS:
            raise ValueError('INVALID_COMMAND: unknown action %r' % (action,))
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise ValueError('INVALID_COMMAND: number must be a positive integer')
        agent, model = validated_spawn_options(agent, model)
        row, created = self.store.enqueue_command(action, number, agent=agent,
                                                  model=model, now=now)
        self.sweep_commands(now=now)
        return row, created

    def ack_command(self, command_id, machine, outcome, fail_reason=None, now=None,
                    execution=None):
        row = self.store.ack_command(command_id, machine, outcome,
                                     fail_reason=fail_reason, now=now,
                                     execution=execution)
        if row is None:
            raise ValueError('COMMAND_STATE: command %s is not CLAIMED by %s'
                             % (command_id, machine))
        return row

    def sweep_commands(self, now=None):
        return self.store.sweep_commands(
            now=now,
            ttl=self.cfg.get('command_ttl', 1800),
            ack_window=self.cfg.get('command_ack_window', 600),
            spawned_window=self.cfg.get('command_spawned_window', 14400))

    def latest_command(self, action, number):
        return self.store.latest_command(action, number)

    def pending_commands(self, now=None):
        # Repair first with this project's configured windows, then list what
        # is still live — a caller never dispatches a command that has aged
        # past its TTL.
        self.sweep_commands(now=now)
        return self.store.pending_commands(now=now)

    def command_progress(self, execution, events, done=False, now=None):
        """Land one batch of the agent-reported session stream for a spawned
        round. The execution must be one this project's ledger knows (a
        command's spawned round, or a claimed execution whose ack went
        missing) — unknown ids are a domain rejection, not silent rows.
        ``done`` is the reporter's terminal marker, recorded as a ledger
        event only; the stream itself carries the round's own end event."""
        if not isinstance(execution, str) or not execution:
            raise ValueError('PROGRESS_EXECUTION: execution must be a non-empty string')
        known = self.store.db.execute(
            "SELECT 1 FROM commands WHERE execution=? "
            "UNION SELECT 1 FROM executions WHERE id=?", (execution, execution)).fetchone()
        if known is None:
            raise ValueError('PROGRESS_EXECUTION: unknown execution %r' % (execution,))
        added = self.store.append_progress(execution, events, now=now)
        if done:
            self.store.event('command.progress_done',
                             {'execution': execution, 'added': added})
        return added
