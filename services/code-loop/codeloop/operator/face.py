"""Operator face: claim/act/report semantics over the machine-level store.

The session-side contract (scripts/operator.py is the stable CLI the prompt
names): acts are whitelist-routed here and audited as ``operator.act`` events
bound to the live round's fencing token; the terminal report is structurally
validated (outcome enum, verb whitelist, act-audit reconciliation, signal
identity) before the round may finalize. Unwired verbs fail honestly at act
time — a session falls back to ESCALATED_HUMAN rather than pretending.

PR2 wiring: ``no_op`` and ``sweep_expired`` execute here; the remaining L1
verbs are declared but fail with OPERATOR_VERB_NOT_WIRED until their serve
side lands (PR3 wires collect_refresh/command_resend, gitea writes follow the
project-credential face). Merge/deploy/restart/delete have no verb at all —
they are WAIT_HUMAN territory by construction.
"""
import json

from . import store as op_store

OUTCOMES = ('RESOLVED', 'MITIGATED_WATCHING', 'ESCALATED_HUMAN',
            'FALSE_POSITIVE_NOOP')
OPERATOR_VERBS = ('no_op', 'sweep_expired', 'collect_refresh', 'command_resend',
                  'github_create_issue', 'github_comment_issue',
                  'gitea_create_issue', 'gitea_comment_issue', 'escalate_human',
                  'wal_checkpoint')
WIRED = {'no_op', 'sweep_expired'}
_EXECUTOR = None


def wire(verbs, executor):
    """Serve-side wiring (cp_server assembly): declare additional verbs
    executable and inject their executor. Unwired verbs keep failing honestly."""
    WIRED.update(verbs)
    global _EXECUTOR
    _EXECUTOR = executor


SELF_PREFIX = 'operator.'


def claim(store, worker, ttl, item_id=None):
    """Claim one READY operator item (long ttl: diagnosis rounds run minutes)."""
    return store.claim('operator', worker=worker, ttl=ttl, item_id=item_id)


def ack_spawned(store, command_id, machine, execution):
    if store.ack_command(command_id, machine, 'SPAWNED', execution=execution) is None:
        raise ValueError('OPERATOR_ACK_LOST: command %s no longer claimed' % command_id)


def ack_failed(store, command_id, machine, reason, claim=None):
    """FAILED ack bound to the round's execution id (review MAJOR-2: the claim
    row carries the execution as ``id``, not a nonexistent ``execution`` key —
    a missing key here made every failure path into a second KeyError that
    masked the original fault in the watcher thread).

    issue #302 defect 1: when the round never claimed (dispatch failure before
    claim — ``claim`` is None), the parent item was left READY and would mute
    the same signal key forever (``active_signal`` sees a READY it will never
    reclaim). Cancel that orphan READY item here, so a re-fired signal for the
    same key can claim a fresh item."""
    execution = claim['id'] if claim else None
    row = store.ack_command(command_id, machine, 'FAILED', fail_reason=reason,
                            execution=execution)
    if claim is None and row is not None and row.get('number') is not None:
        item = op_store.item_for_number(store, row['number'])
        if item is not None and item['state'] == 'READY':
            store.db.execute("UPDATE work_items SET state='CANCELLED' WHERE id=?"
                             " AND state='READY'", (item['id'],))
            store.event('operator.item_recycled', {'execution': None,
                                                   'key': item['key'],
                                                   'reason': reason})


def fail_round(store, execution, token, reason):
    """Liveness verdict: the session died without reporting. Terminal, and
    distinct from a session-reported outcome (the ledger keeps the difference).

    Only live rounds may be failed (BLOCKER-1): a watcher that outlives a
    session that already self-reported SUCCEEDED must not overwrite the
    terminal result — the UPDATE is a no-op when the execution has moved
    past 'RUNNING'/'VERIFYING'. Issue #302 defect 1: the failed verdict now
    also recycles the parent signal item (CANCELLED + lease release), so a
    re-fired same-key signal is not wedged behind a zombie item."""
    op_store.recycle_after_fail(store, execution, token, reason)


def act(store, execution, token, verb, target='', detail='', now=None):
    """Execute one whitelisted L1 verb, audited to the round. Raises ValueError
    with a domain reason code; the CLI surfaces it verbatim."""
    row = store.guard(execution, token)
    if verb not in OPERATOR_VERBS:
        raise ValueError('OPERATOR_VERB_INVALID: %r is not an operator verb' % verb)
    if verb not in WIRED:
        raise ValueError(
            'OPERATOR_VERB_NOT_WIRED: %s awaits its serve-side face; '
            'report ESCALATED_HUMAN instead' % verb)
    receipt = _execute(store, verb, target, detail)
    store.event(SELF_PREFIX + 'act', {'execution': execution, 'verb': verb,
                                      'target': target, 'detail': detail,
                                      'worker': row['worker'], 'receipt': receipt})
    return receipt


def _execute(store, verb, target, detail=''):
    if verb == 'no_op':
        return {'ok': True}
    if verb == 'sweep_expired':
        swept = store.sweep_expired()
        return {'ok': True, 'swept': len(swept)}
    if _EXECUTOR is not None:
        return _EXECUTOR(verb, target, detail)
    raise ValueError('OPERATOR_VERB_NOT_WIRED: %s' % verb)


def _audit_verbs(store, execution):
    return {event['verb'] for event in _act_events(store, execution)}


def _act_events(store, execution):
    # The event payload embeds the execution id as JSON; filter in SQL over
    # the kind index (review MINOR) instead of scanning every event row and
    # keeping a Python-side match — the ledger floor grows with operator age.
    marker = '%%"execution":"%s"%%' % execution
    rows = store.db.execute(
        "SELECT payload FROM events WHERE kind='operator.act' "
        "AND payload LIKE ? ORDER BY created_at", (marker,)).fetchall()
    out = []
    for row in rows:
        payload = json.loads(row['payload'])
        if payload.get('execution') == execution:
            out.append(payload)
    return out


def validate_report(store, execution, result):
    """Structural gate before CAS: outcome enum, verb whitelist, audit
    reconciliation (a declared action must have its operator.act row), and
    signal identity (the report must be about the claimed item). Raises
    ValueError with OPERATOR_REPORT_INVALID."""
    if not isinstance(result, dict):
        raise ValueError('OPERATOR_REPORT_INVALID: report must be a mapping')
    if result.get('outcome') not in OUTCOMES:
        raise ValueError('OPERATOR_REPORT_INVALID: outcome must be one of %s'
                         % ', '.join(OUTCOMES))
    actions = result.get('actions', [])
    if not isinstance(actions, list):
        raise ValueError('OPERATOR_REPORT_INVALID: actions must be a list')
    audited = _audit_verbs(store, execution)
    for action in actions:
        verb = action.get('verb') if isinstance(action, dict) else None
        if verb not in OPERATOR_VERBS:
            raise ValueError('OPERATOR_REPORT_INVALID: verb %r outside whitelist' % verb)
        if verb != 'no_op' and verb not in audited:
            raise ValueError(
                'OPERATOR_REPORT_INVALID: verb %r has no operator.act audit row'
                % verb)
    row = store.db.execute(
        '''SELECT i.key FROM executions e JOIN work_items i ON i.id=e.item_id
           WHERE e.id=?''', (execution,)).fetchone()
    signal = result.get('signal') or ''
    if row is None or signal != row['key']:
        raise ValueError('OPERATOR_REPORT_INVALID: signal %r is not the claimed '
                         'item (%s)' % (signal, row['key'] if row else None))
    if result.get('outcome') == 'ESCALATED_HUMAN' and not actions:
        raise ValueError('OPERATOR_REPORT_INVALID: escalation must carry its '
                         'evidence actions')


def report(store, execution, token, result):
    """Validate then finalize: result on the execution, D1 derivation (no
    outbox obligations for operator rounds → SUCCEEDED), lease release."""
    row = store.guard(execution, token)
    validate_report(store, execution, result)
    with store.transaction():
        store.db.execute('UPDATE executions SET result=? WHERE id=?',
                         (json.dumps(result, ensure_ascii=False), execution))
        resources = store.finalize_locked(execution)
    if resources:
        store._release_locks(resources, execution, row['token'])
    return result
