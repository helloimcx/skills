"""Command ledger: dispatch of operator instructions to worker agents.

Split out of store.py (single-file red line, discover.md §4): the command
segment is a self-contained read/write surface over the ``commands`` table.
Mixin host contract — ``Store`` supplies ``db`` / ``transaction()`` /
``event()``; this class owns no state of its own.

A command is a claimable fact for a deterministic agent, never for an
LLM session: the machine that receives it decides nothing — every guard
(registration, claimability) was already adjudicated by the time the
spawned session goes through the ordinary claim path.

The worker agent executes every action here deterministically
(agent.WorkerAgent.execute dispatches on it); adding one means wiring
its execute branch in the same change.
"""
import time


ACTIVE = ('RUNNING', 'VERIFYING')


class CommandStore:
    COMMAND_ACTIONS = ('review_round', 'repair_round')
    COMMAND_ACTIVE = ('PENDING', 'CLAIMED', 'SPAWNED')

    def enqueue_command(self, action, number, agent=None, model=None, now=None):
        """Record an operator instruction; idempotent while an earlier command
        for the same target is still live. agent/model are pre-validated spawn
        options (Service layer owns the whitelist); the store keeps them dumb.
        Returns (row, created)."""
        now = time.time() if now is None else now
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM commands WHERE action=? AND number=? AND state IN "
                "('PENDING','CLAIMED','SPAWNED') ORDER BY id DESC LIMIT 1",
                (action, number)).fetchone()
            if row is not None:
                return dict(row), False
            cur = self.db.execute(
                "INSERT INTO commands (action, number, state, agent, model, "
                "created_at) VALUES (?,?,'PENDING',?,?,?)",
                (action, number, agent, model, now))
            row = dict(self.db.execute('SELECT * FROM commands WHERE id=?',
                                       (cur.lastrowid,)).fetchone())
            self.event('command.enqueued', {'id': row['id'], 'action': action,
                                            'number': number})
            return row, True

    def pending_commands(self, now=None):
        """Oldest-first live instructions awaiting a worker. Pure read: the
        repair sweep belongs to the caller (``Service.pending_commands`` runs
        it with the project's configured windows first)."""
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM commands WHERE state='PENDING' ORDER BY created_at, id")]

    def mark_dispatched(self, command_id, machine, now=None):
        """Atomically hand a PENDING command to one machine — the single-winner
        point when two agents race. Returns the CLAIMED row or None."""
        now = time.time() if now is None else now
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM commands WHERE id=? AND state='PENDING'",
                (command_id,)).fetchone()
            if row is None:
                return None
            self.db.execute(
                "UPDATE commands SET state='CLAIMED', claimed_by=?, claimed_at=?, "
                "attempts=attempts+1 WHERE id=? AND state='PENDING'",
                (machine, now, command_id))
            row = dict(self.db.execute('SELECT * FROM commands WHERE id=?',
                                       (command_id,)).fetchone())
            self.event('command.dispatched', {'id': command_id, 'machine': machine,
                                              'action': row['action'],
                                              'number': row['number']})
            return row

    def revert_pending(self, command_id, reason=None, now=None):
        """Give a CLAIMED command back when delivery failed before any ack."""
        now = time.time() if now is None else now
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM commands WHERE id=? AND state='CLAIMED'",
                (command_id,)).fetchone()
            if row is None:
                return None
            self.db.execute(
                "UPDATE commands SET state='PENDING', claimed_by=NULL, claimed_at=NULL "
                "WHERE id=?", (command_id,))
            self.event('command.reverted', {'id': command_id, 'reason': reason})
            return dict(self.db.execute('SELECT * FROM commands WHERE id=?',
                                        (command_id,)).fetchone())

    def ack_command(self, command_id, machine, outcome, fail_reason=None, now=None,
                    execution=None):
        """CAS the command from CLAIMED (or SPAWNED, for a post-spawn failure
        like the liveness watcher's PROCESS_DIED — issue #229) to its terminal
        machine-reported state. None means the CAS lost (wrong state or
        claimant) — never retry here. A SPAWNED ack carries the execution it
        started: that id is how the ledger later learns the round ended (see
        sweep_commands)."""
        now = time.time() if now is None else now
        outcome = (outcome or '').upper()
        if outcome not in ('SPAWNED', 'FAILED'):
            raise ValueError('COMMAND_ACK_OUTCOME: outcome must be spawned or failed')
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM commands WHERE id=? AND state IN ('CLAIMED','SPAWNED') "
                "AND claimed_by=?", (command_id, machine)).fetchone()
            if row is None:
                return None
            if outcome == 'SPAWNED':
                # A SPAWNED ack is the CLAIMED→SPAWNED transition only; a second
                # SPAWNED ack (already live) is a CAS loss — the execution id is
                # already on the row.
                if row['state'] != 'CLAIMED':
                    return None
                self.db.execute(
                    "UPDATE commands SET state='SPAWNED', spawned_at=?, fail_reason=NULL, "
                    "execution=? WHERE id=?", (now, execution, command_id))
            else:
                # A FAILED ack must not overwrite a finished round (issue #235):
                # a publish landing inside the watcher's 120s confirm window
                # leaves the command SPAWNED — a terminal execution is a CAS
                # loss. The same guard covers a command stuck in CLAIMED whose
                # SPAWNED ack went missing (issue #255): the ack body still
                # carries the execution the claim started, so the execution row
                # is the ground truth either way.
                exec_id = row['execution'] if row['state'] == 'SPAWNED' else execution
                if exec_id is not None:
                    exec_state = self.db.execute(
                        "SELECT state FROM executions WHERE id=?", (exec_id,)).fetchone()
                    if exec_state is not None and exec_state['state'] not in ACTIVE:
                        return None
                self.db.execute(
                    "UPDATE commands SET state='FAILED', fail_reason=? WHERE id=?",
                    (fail_reason or 'COMMAND_FAILED', command_id))
            self.event('command.acked', {'id': command_id, 'machine': machine,
                                         'outcome': outcome})
            return dict(self.db.execute('SELECT * FROM commands WHERE id=?',
                                        (command_id,)).fetchone())

    def sweep_commands(self, now=None, ttl=1800, ack_window=600, max_attempts=3,
                       spawned_window=14400, progress_retention=7 * 86400):
        """Deterministic repair: PENDING past its lifetime expires (the operator
        re-clicks); CLAIMED past the ack window returns to PENDING — until the
        attempts ratchet caps the retries as a loud FAILED; SPAWNED closes once
        the round it started is no longer running (the execution row is the
        ground truth — the ledger never guesses), or after ``spawned_window``
        when the ack carried no execution id. Terminal commands take their
        reported session stream with them once ``progress_retention`` has
        passed (run-detail replay lives inside that window). Idempotent."""
        now = time.time() if now is None else now
        counts = {'expired': 0, 'requeued': 0, 'failed': 0, 'finished': 0,
                  'progress_cleared': 0}
        with self.transaction():
            cur = self.db.execute(
                "UPDATE commands SET state='EXPIRED', fail_reason='COMMAND_TTL' "
                "WHERE state='PENDING' AND created_at<=?", (now - ttl,))
            counts['expired'] = cur.rowcount
            rows = self.db.execute(
                "SELECT * FROM commands WHERE state='CLAIMED' AND claimed_at<=?",
                (now - ack_window,)).fetchall()
            for row in rows:
                if row['attempts'] >= max_attempts:
                    self.db.execute(
                        "UPDATE commands SET state='FAILED', claimed_by=NULL, "
                        "fail_reason='COMMAND_UNACKED' WHERE id=?", (row['id'],))
                    counts['failed'] += 1
                else:
                    self.db.execute(
                        "UPDATE commands SET state='PENDING', claimed_by=NULL, "
                        "claimed_at=NULL WHERE id=?", (row['id'],))
                    counts['requeued'] += 1
            cur = self.db.execute(
                "UPDATE commands SET state='DONE' WHERE state='SPAWNED' AND ("
                "  (execution IS NOT NULL AND execution NOT IN "
                "     (SELECT id FROM executions WHERE state IN ('RUNNING','VERIFYING')))"
                "  OR (execution IS NULL AND spawned_at<=?))", (now - spawned_window,))
            counts['finished'] = cur.rowcount
            cur = self.db.execute(
                "DELETE FROM progress_events WHERE execution IN ("
                "  SELECT execution FROM commands WHERE execution IS NOT NULL "
                "  AND state IN ('DONE','EXPIRED','FAILED')"
                "  AND coalesce(spawned_at, claimed_at, created_at)<=?"
                "  UNION SELECT id FROM executions WHERE state NOT IN"
                "  ('RUNNING','VERIFYING') AND expires_at<=?)",
                (now - progress_retention, now - progress_retention))
            counts['progress_cleared'] = cur.rowcount
            if counts['expired'] or counts['finished'] or counts['progress_cleared']:
                self.event('command.swept', counts)
        return counts

    def latest_command(self, action, number):
        row = self.db.execute(
            'SELECT * FROM commands WHERE action=? AND number=? '
            'ORDER BY id DESC LIMIT 1', (action, number)).fetchone()
        return dict(row) if row else None
