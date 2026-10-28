"""Gate-signal ledger and report validation (gate-signal redesign).

A verification gate that cannot produce a trustworthy signal about a PR is a
gate state, never a PR verdict. This module owns that state machine: the
structured ``gate`` report field, the per-signature ledger (open -> repaired /
escalated after N failed repairs), and the read-side helpers derive uses.
Pure data in/out — persistence stays with the caller (Service/Sync), so this
module has no dependency on the service layer.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from .runtime import validate_worktree


def gate_key(signature):
    """Stable item/lock key for one env-failure signature."""
    return 'gate:' + hashlib.sha1(signature.encode('utf-8')).hexdigest()[:12]


def validate_gate_shape(gate):
    """Report-side shape check for the ``gate`` field.

    env class carries a non-empty ``signature``; attribution class
    (stale-branch / base-defect) carries no signature and must say what it
    attributed in ``note`` — a gate report with neither is meaningless."""
    if not isinstance(gate, dict) or set(gate) - {'signature', 'commands', 'note'}:
        raise ValueError('INVALID_GATE_REPORT')
    signature = gate.get('signature')
    if signature is not None and (not isinstance(signature, str) or not signature.strip()):
        raise ValueError('INVALID_GATE_REPORT')
    note = gate.get('note', '')
    if not isinstance(note, str):
        raise ValueError('INVALID_GATE_REPORT')
    if not signature and not note.strip():
        raise ValueError('INVALID_GATE_REPORT')
    _validate_gate_commands(gate.get('commands'))


def _validate_gate_commands(commands):
    if commands is None:
        return
    if not isinstance(commands, list) or not all(
            isinstance(c, list) and c and all(isinstance(x, str) and x.strip() for x in c)
            for c in commands):
        raise ValueError('INVALID_GATE_REPORT')


def validate_blocked_evidence(gate, evidence):
    """Bind a Blocked gate report to this round's own verification evidence.

    env class: signature must equal the runner's ``env_signature`` and the
    repair commands must be non-empty (an unrunnable repair would burn three
    guaranteed-failed rounds before escalation). Attribution class (no
    signature): the evidence must carry the matching attribution verdict and
    carries no commands — those exit through sync-base / the discover lane."""
    if not isinstance(gate, dict):
        raise ValueError('GATE_REPORT_REQUIRED')
    if gate.get('signature'):
        if evidence.get('env_signature') != gate['signature']:
            raise ValueError('GATE_SIGNATURE_MISMATCH')
        if not gate.get('commands'):
            raise ValueError('INVALID_GATE_REPORT')
    else:
        if evidence.get('attribution', {}).get('verdict') not in ('stale-branch', 'base-defect'):
            raise ValueError('GATE_SIGNATURE_MISMATCH')
        if gate.get('commands'):
            raise ValueError('INVALID_GATE_REPORT')


def register_blocked(gates, gate):
    """Ledger transition for a reviewer Blocked report (env class only —
    callers must guard on ``gate.get('signature')``)."""
    entry = gates.get(gate['signature']) or {'attempts': 0, 'repair_failures': 0}
    entry.update(state='open', attempts=entry.get('attempts', 0) + 1,
                 commands=gate.get('commands') or entry.get('commands') or [],
                 note=gate.get('note', entry.get('note', '')), updated_at=time.time())
    gates[gate['signature']] = entry
    return entry


def apply_repair_result(gates, signature, decision):
    entry = gates.get(signature) or {}
    if decision == 'RESOLVED':
        entry.update(state='repaired', resolved_at=time.time())
    else:
        entry.update(repair_failures=entry.get('repair_failures', 0) + 1, updated_at=time.time())
    gates[signature] = entry
    return entry


def escalate(gates, max_attempts):
    """Escalate exhausted signatures (open -> escalated). Returns the changed
    signatures so the caller can decide whether to persist."""
    changed = [sig for sig, entry in gates.items()
               if entry.get('state') == 'open'
               and entry.get('repair_failures', 0) >= max_attempts]
    for sig in changed:
        gates[sig]['state'] = 'escalated'
        gates[sig]['escalated_at'] = time.time()
    return changed


def resolve_signatures(gates, signatures):
    """A green run proves the gate healthy again even if no repair reported."""
    resolved = [sig for sig in signatures
                if gates.get(sig, {}).get('state') in ('open', 'escalated')]
    for sig in resolved:
        gates[sig]['state'] = 'repaired'
        gates[sig]['resolved_at'] = time.time()
    return resolved


def open_signatures(gates):
    return {sig for sig, entry in gates.items()
            if entry.get('state') in ('open', 'escalated')}


def escalations(gates, max_attempts):
    """Read-only view for the discover payload.

    An exhausted ``open`` entry reports as ``escalated`` here — the sync
    transaction applies that transition right after derive, so reporting the
    pre-transition value would flip the payload (and re-mint the discover
    item) on the following cycle for no information."""
    return [{'signature': sig,
             'repair_failures': entry.get('repair_failures', 0),
             'note': entry.get('note', ''),
             'state': 'escalated' if entry.get('state') == 'open' else entry.get('state')}
            for sig, entry in sorted(gates.items())
            if entry.get('repair_failures', 0) >= max_attempts
            and entry.get('state') != 'repaired']


# -- derive-side PR decisions (kept here so derive stays orchestration) -----

def terminal_verdicts(store, pr, changes):
    """Fold our own published terminal verdicts for this exact head into the
    decision inputs.

    Terminal verdicts at this head are facts, not reopeners: an Approved PR
    waits for the human merge and is never re-litigated. Changes requested
    and Validation failed stay separate so a failed check can be attributed
    before entering the PR repair lane."""
    approved_here = False
    validation_failed = False
    head = pr['head']['sha']
    for row in store.db.execute(
            "SELECT payload FROM outbox WHERE action='review_comment' AND state='SENT'"):
        body = json.loads(row['payload'])
        if body.get('number') != pr['number'] or ('Head: ' + head) not in body.get('body', ''):
            continue
        if 'Decision: Changes requested' in body['body']:
            changes = True
        elif 'Decision: Approved for manual merge' in body['body']:
            approved_here = True
        elif 'Decision: Validation failed' in body['body']:
            validation_failed = True
    return changes, validation_failed, approved_here


def _at_this_head(signal, pr):
    return signal is not None and signal.get('head') == pr['head']['sha']


def attributed_stale(signal, pr):
    return _at_this_head(signal, pr) and signal.get('attribution') == 'stale-branch'


def is_frozen(signal, pr, open_gates, base_sha):
    """No re-litigation while the gate cannot speak about this PR.

    env gate broken: frozen until the gate item (or an out-of-band fix proven
    by a later green run) resolves it. Base itself broken at the tip this PR
    verified against: frozen until the base actually advances."""
    if not _at_this_head(signal, pr):
        return False
    signature = signal.get('env_signature')
    if signature and signature in open_gates:
        return True
    return (signal.get('attribution') == 'base-defect'
            and signal.get('base_tip_sha') == base_sha)


def attach_gate_state(payload, signal, pr, gates):
    """The gate state rides the payload: once the signature leaves the open
    set, the review item re-derives under a NEW version — a same-version row
    would stay dead at its terminal state and the PR would never re-verify."""
    if not _at_this_head(signal, pr):
        return
    signature = signal.get('env_signature')
    if signature and signature in gates:
        payload['gate'] = {'signature': signature, 'state': gates[signature].get('state')}


CLOSES_RE = re.compile(r'(?i)\b(?:closes?|fix(?:es)?|resolves?)\s+#(\d+)\b')


def linked_issues(prs):
    """Issue numbers each PR body claims to close (Closes/Fixes/Resolves)."""
    return {pr['number']: sorted({int(n) for n in CLOSES_RE.findall(pr.get('body') or '')})
            for pr in prs}


_ACTION_SCOPE = {'discover': ('create_issue', 'comment_issue', 'close_issue'),
                 'handler': ('create_pr', 'edit_pr', 'close_issue', 'comment_pr'),
                 'reviewer': ('review_comment',)}

_PAYLOAD_FIELDS = {'create_issue': {'title', 'body'}, 'comment_issue': {'number', 'body'},
                   'create_pr': {'title', 'body', 'head', 'base'},
                   'edit_pr': {'number', 'title', 'body'}, 'review_comment': {'number', 'body'},
                   'comment_pr': {'number', 'body'}, 'close_issue': {'number'}}


def _check_payload(action, p, kind):
    fields = {'number', 'body'} if (action == 'close_issue' and kind == 'discover') \
        else _PAYLOAD_FIELDS.get(action)
    if fields is None or set(p) != fields:
        raise ValueError('INVALID_PUBLICATION_PAYLOAD')
    for key, value in p.items():
        if key == 'number' and (type(value) is not int or value <= 0):
            raise ValueError('INVALID_PUBLICATION_NUMBER')
        if key != 'number' and (not isinstance(value, str) or (key != 'body' and not value.strip())):
            raise ValueError('INVALID_PUBLICATION_TEXT')


def _decision_pending(issue):
    labels = [x.get('name', '') if isinstance(x, dict) else x for x in issue.get('labels', [])]
    return ('needs-human' in labels or '[❓needs-human]' in labels
            or issue['title'].startswith('[Gap]')
            or issue['title'].startswith('[❓needs-human]'))


def _check_discover_close(service, p, issue, snapshot):
    """Fallback closure of a resolved issue no PR covers: the target must be
    observation-closable — never decision-pending, never work in flight,
    never handler's POST_MERGE_VERIFY territory."""
    if issue['state'] != 'open':
        raise ValueError('ISSUE_NOT_OPEN')
    if not (p.get('body') or '').strip():
        raise ValueError('CLOSE_EVIDENCE_REQUIRED')
    if _decision_pending(issue):
        raise ValueError('CLOSE_TARGET_FORBIDDEN')
    linked = linked_issues(snapshot['prs'])
    related = [pr for pr in snapshot['prs'] if p['number'] in linked[pr['number']]]
    if any(pr['state'] == 'open' for pr in related):
        raise ValueError('CLOSE_TARGET_IN_FLIGHT')
    if any(pr.get('merged') and pr['base']['ref'] == service.cfg['base'] for pr in related):
        raise ValueError('CLOSE_TARGET_POST_MERGE')


def _check_close_target(service, item, source, action, p, snapshot, issue):
    if action != 'close_issue':
        return
    if item['kind'] == 'discover':
        return _check_discover_close(service, p, issue, snapshot)
    if (source['task'] != 'POST_MERGE_VERIFY'
            or p.get('number') != source['issue']['number']):
        raise ValueError('POST_MERGE_EVIDENCE_REQUIRED')


def _check_pr_targets(source, action, p, decision):
    if action in ('edit_pr', 'review_comment', 'comment_pr') \
            and p.get('number') != source.get('pr', {}).get('number'):
        raise ValueError('WRONG_PR_TARGET')
    if action == 'comment_pr' and decision != 'BLOCKED':
        # The PR comment channel exists solely for BLOCKED visibility;
        # approval flows through push + edit_pr instead.
        raise ValueError('ACTION_DECISION_MISMATCH')
    if action == 'create_pr' and source['task'] != 'ISSUE_IMPLEMENTATION':
        raise ValueError('USE_EXISTING_PR')


def authorize(service, item, source, result):
    validator = getattr(service.platform, 'validate_action', None)
    actions = result.get('actions')
    if actions is None:
        actions = [{'action': result['action'], 'payload': result.get('payload', {})}]
    for entry in actions:
        action, p = entry['action'], entry['payload']
        if not isinstance(p, dict):
            raise ValueError('INVALID_PAYLOAD')
        if action not in _ACTION_SCOPE[item['kind']]:
            raise ValueError('ROLE_ACTION_FORBIDDEN')
        _check_payload(action, p, item['kind'])
        if validator is not None:
            validator(action, p)
        snapshot, issue = None, None
        if action in ('comment_issue', 'close_issue'):
            snapshot = service.snapshot()
            issue = next((i for i in snapshot['issues'] if i['number'] == p.get('number')), None)
            if issue is None:
                raise ValueError('ISSUE_REQUIRED_NOT_PR')
        _check_close_target(service, item, source, action, p, snapshot, issue)
        _check_pr_targets(source, action, p, result['decision'])


def _require_independent_judgment(service, e, context, error):
    judgments = service.store.evidence(e['id'], 'judgment')
    judgment = json.loads(judgments[-1]['payload']) if judgments else {}
    if (not judgments or judgment.get('reviewer') == e['worker']
            or judgment.get('head') != context['head']
            or judgment.get('input_version') != e['item_version']):
        raise ValueError(error)


def _gate_close_issue(service, e, source, result, context):
    """POST_MERGE_VERIFY closure: every linked PR merged, this round's own
    verification green at the merge commit, and an independent judgment."""
    if result['decision'] != 'RESOLVED' or not all(
            p.get('merged') and p['base']['ref'] == service.cfg['base'] for p in source['prs']):
        raise ValueError('RESOLUTION_NOT_PROVEN')
    if not e['worktree']:
        raise ValueError('POST_MERGE_WORKTREE_REQUIRED')
    checks = service.store.evidence(e['id'], 'verification')
    if not checks:
        raise ValueError('POST_MERGE_VERIFICATION_REQUIRED')
    ev = json.loads(checks[-1]['payload'])
    if context is None:
        context = validate_worktree(service.cfg, e['worktree'], e['branch'])
    if (context['head'] != source['base_sha'] or not ev.get('passed')
            or ev.get('head') != context['head'] or ev.get('input_version') != e['item_version']):
        raise ValueError('POST_MERGE_VERIFICATION_STALE')
    _require_independent_judgment(service, e, context,
                                  'INDEPENDENT_POST_MERGE_REVIEW_REQUIRED')


def _gate_required_checks(service, source, context):
    pr = source['pr']
    if context['head'] != pr['head']['sha'] or pr.get('mergeable') is not True:
        raise ValueError('PR_STATE_UNKNOWN_OR_CONFLICT')
    latest = {}
    for check in pr.get('checks', []):
        name = check.get('context')
        if name not in latest or check.get('id', 0) > latest[name].get('id', 0):
            latest[name] = check
    if any(latest.get(name, {}).get('status') != 'success'
           for name in service._exam()['required_checks']):
        raise ValueError('REQUIRED_CHECKS_NOT_PASS')


def publish_gate(service, e, item, source, result, context=None):
    """Gate the publication of a round: role action scope, decision/evidence
    agreement, and (for reviewer approvals) the PR's own state."""
    service.authorize(item, source, result)
    decision = result['decision']
    kind = item['kind']
    if kind == 'discover':
        if decision != 'CONFIRMED':
            raise ValueError('CONFIRMED_REQUIRED')
        return
    if result['action'] == 'comment_pr':
        # BLOCKED visibility comment: judge-and-publish only — by definition
        # there is no verification evidence or push behind a blocked round.
        return
    if result['action'] == 'close_issue':
        return _gate_close_issue(service, e, source, result, context)
    if kind == 'reviewer' and decision != 'Approved for manual merge':
        return
    if kind == 'handler' and decision != 'APPROVE':
        raise ValueError('APPROVE_REQUIRED')
    context = _require_green_evidence(service, e, context)
    _require_independent_judgment(service, e, context, 'JUDGMENT_STALE: report after verification')
    if kind == 'reviewer':
        _gate_required_checks(service, source, context)


def _require_green_evidence(service, e, context):
    checks = service.store.evidence(e['id'], 'verification')
    if not checks:
        raise ValueError('RUNNER_EVIDENCE_REQUIRED')
    ev = json.loads(checks[-1]['payload'])
    if context is None:
        context = validate_worktree(service.cfg, e['worktree'], e['branch'])
    if not ev['passed'] or ev['head'] != context['head'] or ev['input_version'] != e['item_version']:
        raise ValueError('VERIFICATION_FAILED_OR_STALE')
    return context


# -- report-side validation (kept here so Service.report stays orchestration)

_REPORT_FIELDS = {'decision', 'reviewer', 'summary', 'action', 'payload', 'actions', 'coverage', 'gate'}
_EXPECTED_DECISIONS = {
    'discover': ('CONFIRMED', 'REJECTED', 'NEEDS_MORE_EVIDENCE'),
    'handler': ('APPROVE', 'REQUEST_CHANGES', 'BLOCKED', 'RESOLVED'),
    'reviewer': ('Approved for manual merge', 'Changes requested', 'Blocked', 'Validation failed')}


def _validate_coverage_field(item, result):
    """覆盖度核算（reviewer 判词）：Approved 必带且 ok=true——diff 里不得有既未
    审也无跳过理由的文件（reviewer.md 覆盖度门禁）。其余判词/角色可选，提供时
    同样过一致性校验。"""
    from .report_coverage import validate_coverage
    coverage = result.get('coverage')
    approved = (item['kind'] == 'reviewer'
                and result['decision'] == 'Approved for manual merge')
    if coverage is not None:
        validate_coverage(coverage)
        if approved and not coverage['ok']:
            raise ValueError('COVERAGE_INCOMPLETE')
    elif approved:
        raise ValueError('COVERAGE_REQUIRED')


def _validate_gate_repair(service, item, payload, result, eid):
    """A gate repair either proves itself or counts as a failed attempt —
    APPROVE/REQUEST_CHANGES are meaningless here."""
    if item['kind'] != 'handler' or payload.get('task') != 'GATE_REPAIR':
        return
    if result['decision'] not in ('RESOLVED', 'BLOCKED'):
        raise ValueError('INVALID_DECISION')
    if result['decision'] == 'RESOLVED':
        checks = service.store.evidence(eid, 'verification')
        if not checks or not json.loads(checks[-1]['payload']).get('passed'):
            raise ValueError('GATE_REPAIR_PROOF_REQUIRED')


def _require_independent_reviewer(service, e, item, payload, result):
    if result['reviewer'] == e['worker']:
        raise ValueError('INDEPENDENT_REVIEW_REQUIRED')
    if item['kind'] != 'reviewer' or not payload.get('pr'):
        return
    developers = service.store.db.execute(
        '''SELECT e.worker FROM executions e JOIN work_items w ON w.id=e.item_id
             WHERE w.kind='handler' AND e.branch=?''',
        (payload['pr']['head']['ref'],)).fetchall()
    if any(r['worker'] in (e['worker'], result['reviewer']) for r in developers):
        raise ValueError('DEVELOPER_CANNOT_ACCEPT_OWN_PR')


def _validate_blocked_visibility(item, payload, result):
    # A BLOCKED conflict resolution must carry its visibility comment, or
    # the verdict stays invisible on the platform (handler.md: 非静默红线).
    if (item['kind'] == 'handler' and result['decision'] == 'BLOCKED'
            and payload.get('task') == 'PR_CONFLICT_RESOLUTION'
            and result.get('action') != 'comment_pr'):
        raise ValueError('BLOCKED_VISIBILITY_REQUIRED')


def _validate_review_comment_required(service, item, payload, result):
    """Reviewer verdicts that must reach the platform (issue #223): a first
    review of a PR — or a re-derived review whose head has moved — must carry
    action=review_comment, or the verdict is silently dropped: the report
    derives SUCCEEDED (D1) and the PR never gets its comment. Re-derived
    items (PR head unchanged, a SENT verdict body already names this exact
    head) may report without an action to confirm the existing verdict —
    the same ``'Head: <sha>'`` match terminal_verdicts uses, so a verdict
    dropped at an old head can never exempt a new head."""
    if item['kind'] != 'reviewer' or not payload.get('pr'):
        return
    if result['decision'] not in ('Approved for manual merge', 'Changes requested', 'Validation failed'):
        return
    action = result.get('action')
    if action == 'review_comment':
        return
    if action:
        # A non-review_comment action from a reviewer is out of scope; the
        # authorize gate reports it as ROLE_ACTION_FORBIDDEN. Returning here
        # keeps that error clear instead of masking it with
        # REVIEW_COMMENT_REQUIRED.
        return
    number = payload['pr']['number']
    head = payload['pr']['head']['sha']
    sent = service.store.db.execute(
        "SELECT payload FROM outbox WHERE action='review_comment' AND state='SENT'").fetchall()
    for (row,) in sent:
        try:
            body = json.loads(row)
        except (ValueError, TypeError):
            continue
        if body.get('number') == number and ('Head: ' + head) in body.get('body', ''):
            return
    raise ValueError('REVIEW_COMMENT_REQUIRED: first review of PR #%s at head %s must carry action=review_comment' % (number, head))


def validate_report(service, e, item, payload, eid, result):
    """Field/decision/gate validation for a report; returns the gate report."""
    if not isinstance(result, dict) or set(result) - _REPORT_FIELDS:
        raise ValueError('INVALID_REPORT_FIELDS')
    if not all(isinstance(result.get(k), str) and result[k].strip()
               for k in ('decision', 'reviewer', 'summary')):
        raise ValueError('DECISION_REVIEWER_SUMMARY_REQUIRED')
    if 'actions' in result:
        if 'action' in result or 'payload' in result:
            raise ValueError('ACTION_FIELDS_MUTUALLY_EXCLUSIVE')
        actions = result['actions']
        if item['kind'] != 'discover' or not isinstance(actions, list) or not 2 <= len(actions) <= 10:
            raise ValueError('INVALID_ACTIONS')
        if result['decision'] != 'CONFIRMED':
            raise ValueError('CONFIRMED_REQUIRED')
        seen = set()
        total_bytes = 0
        for entry in actions:
            if not isinstance(entry, dict) or set(entry) != {'action', 'payload'}:
                raise ValueError('INVALID_ACTION_ENTRY')
            if entry['action'] != 'create_issue':
                raise ValueError('BATCH_ACTION_FORBIDDEN')
            p = entry['payload']
            if not isinstance(p, dict):
                raise ValueError('INVALID_PAYLOAD')
            _check_payload(entry['action'], p, item['kind'])
            if len(p['title']) > 256 or len(p['title'].encode('utf-8')) > 256:
                raise ValueError('INVALID_ISSUE_TITLE_LENGTH')
            body_bytes = len(p['body'].encode('utf-8'))
            if body_bytes > 65536:
                raise ValueError('INVALID_ISSUE_BODY_LENGTH')
            total_bytes += len(p['title'].encode('utf-8')) + body_bytes
            fingerprint = (p['title'].strip().casefold(), p['body'].strip())
            if fingerprint in seen:
                raise ValueError('DUPLICATE_BATCH_ISSUE')
            seen.add(fingerprint)
        if total_bytes > 500000:
            raise ValueError('BATCH_PAYLOAD_TOO_LARGE')
    gate = result.get('gate')
    if gate is not None:
        validate_gate_shape(gate)
    if item['kind'] == 'reviewer' and result['decision'] == 'Blocked':
        # Blocked is a gate report, never a PR verdict: it must be
        # substantiated by this round's own verification evidence.
        latest = service.store.evidence(eid, 'verification')
        validate_blocked_evidence(gate, json.loads(latest[-1]['payload']) if latest else {})
    _require_independent_reviewer(service, e, item, payload, result)
    if result['decision'] not in _EXPECTED_DECISIONS[item['kind']]:
        raise ValueError('INVALID_DECISION')
    _validate_coverage_field(item, result)
    _validate_blocked_visibility(item, payload, result)
    _validate_review_comment_required(service, item, payload, result)
    _validate_gate_repair(service, item, payload, result, eid)
    return gate


def changes_requested(pr):
    """Latest review per actor: any REQUEST_CHANGES counts."""
    reviews = {}
    for r in pr.get('reviews', []):
        reviews[str((r.get('user') or {}).get('id', r.get('id')))] = r
    return any(r.get('state', '').upper() in ('REQUEST_CHANGES', 'REQUESTED_CHANGES', 'CHANGES_REQUESTED')
               for r in reviews.values())
