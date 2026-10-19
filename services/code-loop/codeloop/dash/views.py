"""Dashboard store views layer: queues, exceptions, funnel, kanban, trends.

Derived operational views read from collected facts only; every conclusion
is computed at query time. ``_verdicts._cp_verdicts`` (module attribute) is
the trends-side call so the defining module stays the single patch point.
"""
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import verdicts as _verdicts
from .schema import NEEDS_HUMAN, is_agent_comment, parse_ts
from .verdicts import latest_verdict, latest_verdict_detail, verdicts_for


def _project_map(db):
    return {row['id']: row for row in db.execute('SELECT * FROM projects')}


def _pr_rows(db, pid=None, state='open'):
    query = ('SELECT p.*, pr.org_repo FROM prs p JOIN projects pr ON pr.id=p.project_id')
    clauses, params = [], []
    if pid is not None:
        clauses.append('p.project_id=?')
        params.append(pid)
    if state == 'open':
        clauses.append("p.state='open' AND p.merged=0")
    elif state == 'merged':
        clauses.append('p.merged=1')
    elif state == 'closed':
        clauses.append("p.state='closed'")
    if clauses:
        query += ' WHERE ' + ' AND '.join(clauses)
    return [dict(r) for r in db.execute(query, params)]


def pending_merges(db, now=None):
    """Open PRs whose latest verdict approves them for manual merge."""
    rows = []
    open_prs = _pr_rows(db, state='open')
    verdicts = verdicts_for(db, open_prs)
    for pr in open_prs:
        detail = latest_verdict_detail(db, pr['project_id'], pr['number'],
                                       cp_verdicts=verdicts[pr['project_id']])
        if detail is None or detail['verdict'] != 'Approved for manual merge':
            continue
        pr['verdict'] = detail['verdict']
        pr['verdict_at'] = detail['at']
        pr['verdict_poster'] = detail['poster']
        pr['verdict_body'] = detail['body']
        pr['waiting_seconds'] = max(0.0, (now or 0) - detail['at']) if now else None
        rows.append(pr)
    rows.sort(key=lambda r: r.get('waiting_seconds') or 0, reverse=True)
    return rows


def _is_needs_human(issue):
    haystack = (issue['title'] or '') + ' ' + ' '.join(json.loads(issue['labels'] or '[]'))
    return NEEDS_HUMAN in haystack


def _latest_user_reply(db, pid, number):
    row = db.execute('SELECT created_at FROM user_replies '
                     'WHERE project_id=? AND number=? '
                     'ORDER BY created_at DESC, id DESC LIMIT 1',
                     (pid, number)).fetchone()
    return row['created_at'] if row else None


def issue_detail(db, org_repo, number):
    """Full context for the decision detail view: the whole issue row plus
    every comment, oldest first. None when project or number is unknown."""
    row = db.execute('SELECT i.*, pr.org_repo FROM issues i '
                     'JOIN projects pr ON pr.id=i.project_id '
                     'WHERE pr.org_repo=? AND i.number=?', (org_repo, number)).fetchone()
    if row is None:
        return None
    issue = dict(row)
    issue['comments'] = [dict(c) for c in db.execute(
        'SELECT poster, body, created_at FROM comments '
        "WHERE project_id=? AND kind='issue' AND number=? "
        'ORDER BY created_at, id', (row['project_id'], number))]
    return issue


def _latest_platform_user_comment(db, pid, number):
    """Newest collected platform comment that is not an agent signature. A
    human replying directly on Gitea posts under the same account as the
    loops, so only the absence of the agent marker identifies it as a user
    decision."""
    for row in db.execute('SELECT body, created_at FROM comments '
                          "WHERE project_id=? AND kind='issue' AND number=? "
                          'ORDER BY created_at DESC, id DESC',
                          (pid, number)):
        if not is_agent_comment(row['body']):
            return row['created_at']
    return None


def pending_decisions(db, now=None):
    """Open issues tagged [❓needs-human] (title prefix or label) with last comment.
    Aging counts from issue creation: updated_at moves on every batch comment
    and would hide week-old backlogs behind a fresh-looking timestamp.
    Issues the user already replied to — via the dashboard proxy (user_replies)
    or directly on Gitea (a platform comment without the agent marker) — are
    excluded; the ball is with the handler."""
    rows = []
    query = ('SELECT i.*, pr.org_repo FROM issues i JOIN projects pr ON pr.id=i.project_id '
             "WHERE i.state='open'")
    for row in db.execute(query):
        if not _is_needs_human(row):
            continue
        issue = dict(row)
        last = db.execute('SELECT poster, body, created_at FROM comments '
                          "WHERE project_id=? AND kind='issue' AND number=? "
                          'ORDER BY created_at DESC, id DESC LIMIT 1',
                          (row['project_id'], row['number'])).fetchone()
        issue['last_comment'] = dict(last) if last else None
        reply_at = parse_ts(_latest_user_reply(db, row['project_id'], row['number']))
        platform_reply_at = parse_ts(
            _latest_platform_user_comment(db, row['project_id'], row['number']))
        if platform_reply_at is not None and (reply_at is None or platform_reply_at > reply_at):
            reply_at = platform_reply_at
        last_at = parse_ts(issue['last_comment']['created_at']) if issue['last_comment'] else None
        if reply_at is not None and (last_at is None or reply_at >= last_at):
            continue
        created = parse_ts(issue['created_at'])
        issue['age_basis'] = 'created_at'
        issue['waiting_seconds'] = max(0.0, (now or 0) - created) if (now and created) else None
        rows.append(issue)
    rows.sort(key=lambda r: r.get('waiting_seconds') or 0, reverse=True)
    return rows


def _latest_agent_comment_at(db, pid, number):
    """Newest collected issue comment that carries the agent identity marker."""
    for row in db.execute('SELECT body, created_at FROM comments '
                          "WHERE project_id=? AND kind='issue' AND number=? "
                          'ORDER BY created_at DESC, id DESC',
                          (pid, number)):
        if is_agent_comment(row['body']):
            return row['created_at']
    return None


def pending_consumption(db, now=None):
    """Open issues tagged [❓needs-human] whose newest word is the user's.

    These are decisions already made by the user that the handler has not
    consumed yet — the tag is still on, so nothing was consumed. Exact
    complement of pending_decisions, which drops exactly these rows on the
    assumption that the ball is with the handler; with no view on this side
    either, the reply falls between the two (2026-09-11: a user reply sat
    unconsumed for 20+ rounds). Deterministic arrival detection: the handler
    reads this list instead of re-reading every WAIT_HUMAN thread."""
    rows = []
    query = ('SELECT i.*, pr.org_repo FROM issues i JOIN projects pr ON pr.id=i.project_id '
             "WHERE i.state='open'")
    for row in db.execute(query):
        if not _is_needs_human(row):
            continue
        pid, number = row['project_id'], row['number']
        agent_at = parse_ts(_latest_agent_comment_at(db, pid, number))
        user_at = parse_ts(_latest_user_reply(db, pid, number))
        platform_at = parse_ts(_latest_platform_user_comment(db, pid, number))
        if platform_at is not None and (user_at is None or platform_at > user_at):
            user_at = platform_at
        if user_at is None or (agent_at is not None and agent_at >= user_at):
            continue
        issue = dict(row)
        issue['user_reply_at'] = user_at
        issue['agent_reply_at'] = agent_at
        issue['waiting_seconds'] = max(0.0, (now or 0) - user_at) if now else None
        rows.append(issue)
    rows.sort(key=lambda r: r['user_reply_at'])
    return rows


def exceptions(db, now=None):
    """Everything that needs a human operator: outbox limbo, dead leases, blocked PRs."""
    items = []
    for row in db.execute("SELECT o.*, pr.org_repo FROM outbox o JOIN projects pr "
                          "ON pr.id=o.project_id WHERE o.state IN ('SENDING','UNKNOWN')"):
        items.append({'type': 'outbox', 'severity': 'error', 'key': 'outbox-%s' % row['cp_id'],
                      'org_repo': row['org_repo'], 'state': row['state'],
                      'action': row['action'], 'created_at': row['created_at'],
                      'html_url': '', 'title': 'outbox #%s %s' % (row['cp_id'], row['action']),
                      'message': 'outbox %s action=%s（需人工 reconcile）'
                                 % (row['state'], row['action'])})
    for row in db.execute('SELECT l.*, pr.org_repo FROM leases l JOIN projects pr '
                          'ON pr.id=l.project_id'):
        expires = parse_ts(row['expires_at'])
        if expires is not None and expires < (now or 0):
            items.append({'type': 'lease_expired', 'severity': 'warn',
                          'key': 'lease-' + (row['resource'] or ''),
                          'org_repo': row['org_repo'], 'resource': row['resource'],
                          'execution_cp_id': row['execution_cp_id'],
                          'expires_at': row['expires_at'], 'html_url': '',
                          'title': '租约过期 %s' % (row['resource'] or ''),
                          'message': '租约已过期 resource=%s（下轮 claim 自动接管/重跑；仅 outbox 未决需人工 reconcile）'
                                     % row['resource']})
    open_prs = _pr_rows(db, state='open')
    verdicts = verdicts_for(db, open_prs)
    for pr in open_prs:
        detail = latest_verdict_detail(db, pr['project_id'], pr['number'],
                                       cp_verdicts=verdicts[pr['project_id']])
        if detail is not None and detail['verdict'] in ('Blocked', 'Validation failed'):
            items.append({'type': 'pr', 'severity': 'warn', 'key': 'pr-%d' % pr['number'],
                          'org_repo': pr['org_repo'],
                          'number': pr['number'], 'title': pr['title'],
                          'html_url': pr['html_url'], 'verdict': detail['verdict'],
                          'verdict_body': detail['body'], 'mergeable': pr['mergeable'],
                          'message': 'PR #%d %s：%s' % (pr['number'], pr['title'],
                                                        detail['verdict'])})
    return items


def funnel(db, pid):
    """Issue/PR pipeline stage counts for one project."""
    open_issues = db.execute("SELECT COUNT(*) FROM issues WHERE project_id=? AND state='open'",
                             (pid,)).fetchone()[0]
    open_numbers = {r['number'] for r in db.execute(
        "SELECT number FROM issues WHERE project_id=? AND state='open'", (pid,))}
    prs = _pr_rows(db, pid=pid, state='open')
    verdicts = verdicts_for(db, prs)
    in_development = set()
    awaiting = requested = blocked = ready = 0
    for pr in prs:
        for number in json.loads(pr['issue_numbers'] or '[]'):
            if number in open_numbers:
                in_development.add(number)
        verdict = latest_verdict(db, pid, pr['number'],
                                 cp_verdicts=verdicts[pid])
        if verdict is None:
            awaiting += 1
        elif verdict[0] == 'Approved for manual merge':
            ready += 1
        elif verdict[0] == 'Changes requested':
            requested += 1
        else:
            blocked += 1
    return {'open_issues': open_issues, 'in_development': len(in_development),
            'awaiting_review': awaiting, 'changes_requested': requested,
            'blocked': blocked, 'ready_merge': ready}


KANBAN_COLUMNS = ('todo', 'running', 'review', 'reviewing', 'ready', 'merged',
                  'blocked')
MERGED_CAP = 15  # recent merged cards shown on the board


def _live_cp_view(project, now):
    """One read-only connection to the project's machine-local control-plane
    store per render, for the facts the board reads out of it: RUNNING
    reviewer executions (a claim lands in that SQLite the moment a session
    takes its item, so the board must not wait for a collect cycle to show who
    is reviewing what; only live leases, the same liveness rule the control
    plane reclaims by — an expired execution is a reclaim target, not running
    work, and stays visible in ``exceptions``), RUNNING handler executions
    (the repair round's slot in 处理中, same liveness rule) and the latest
    ``review_round`` / ``repair_round`` command per PR (the click's outcome,
    read from the ledger that owns it rather than remembered by the page).
    Returns None when the store cannot be read, which means "fall back to the
    collected mirror" for the executions and "show no command state" — never
    a guessed outcome."""
    config_path = project['config_path'] or ''
    if not config_path:
        return None
    cp_db = Path(config_path).parent / 'state' / 'control-plane.sqlite'
    if not cp_db.is_file():
        return None
    threshold = now or time.time()
    try:
        source = sqlite3.connect('file:%s?mode=ro' % cp_db.as_posix(), uri=True)
    except sqlite3.Error:
        return None
    try:
        source.row_factory = sqlite3.Row
        try:
            reviewing = [dict(row) for row in source.execute(
                "SELECT wi.key AS wi_key, wi.payload AS wi_payload, ex.id AS execution, "
                "ex.worker, ex.branch, "
                "ex.created_at, ex.expires_at, ex.worktree "
                "FROM executions ex JOIN work_items wi ON wi.id=ex.item_id "
                "WHERE ex.state='RUNNING' AND wi.kind='reviewer' AND ex.expires_at > ?",
                (threshold,))]
        except sqlite3.Error:
            return None
        running = _live_handler_executions(source, threshold)
        # The ledger is younger than the executions table: a store the control
        # plane has not reopened yet has no commands table at all. That costs
        # the command state and nothing else.
        commands, repair_commands = _live_commands(source)
        return {'reviewing': reviewing, 'commands': commands,
                'running': running, 'repair_commands': repair_commands}
    finally:
        source.close()


def _live_handler_executions(source, threshold):
    """RUNNING handler executions from the same live store — the repair
    round's claim lands here the moment a session takes the item. Degrades to
    empty (the collected mirror takes over) rather than poisoning the whole
    live view: a schema surprise in one query must not unshow the reviewer."""
    try:
        return [dict(row) for row in source.execute(
            "SELECT wi.key AS wi_key, wi.payload AS wi_payload, ex.id AS execution, "
            "ex.worker, ex.branch, "
            "ex.created_at, ex.expires_at, ex.worktree "
            "FROM executions ex JOIN work_items wi ON wi.id=ex.item_id "
            "WHERE ex.state='RUNNING' AND wi.kind='handler' AND ex.expires_at > ?",
            (threshold,))]
    except sqlite3.Error:
        return []


def _live_commands(source):
    """Latest review_round and repair_round command per PR number. Both
    degrade together: no commands table means no click state on any card."""
    commands, repair_commands = {}, {}
    try:
        for row in source.execute(
                "SELECT number, state, fail_reason, created_at FROM commands "
                "WHERE action='review_round' AND id IN (SELECT MAX(id) FROM commands "
                "WHERE action='review_round' GROUP BY number)"):
            commands[row['number']] = dict(row)
        for row in source.execute(
                "SELECT number, state, fail_reason, created_at FROM commands "
                "WHERE action='repair_round' AND id IN (SELECT MAX(id) FROM commands "
                "WHERE action='repair_round' GROUP BY number)"):
            repair_commands[row['number']] = dict(row)
    except sqlite3.Error:
        return {}, {}
    return commands, repair_commands


def _cp_base(project):
    """The project's control-plane base branch, resolved the way the control
    plane resolves it — ``cp/runtime.py`` setdefaults an absent key to 'main' —
    so the board's scope verdict matches the derivation the CP actually runs.
    The config file is read at render time instead of reusing the collected
    ``projects.base`` column: that column is a collect-cycle-old copy of this
    same file, and this value decides whether a button is offered at all.
    None when the config cannot be read (or carries no usable base), which the
    scope verdict must carry as "unknown": an unreadable file is not evidence
    that a PR is out of scope."""
    path = project['config_path'] or ''
    if not path:
        return None
    try:
        cfg = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict):
        return None
    base = cfg.get('base', 'main')
    return base.strip() if isinstance(base, str) and base.strip() else None


def _review_scope(pr, cp_base):
    """Whether the control plane can derive a review item for this PR at all:
    it derives only for open, non-draft PRs whose base is the project's base
    (``cp/service.py``), so a card outside that scope would carry a button that
    must fail. Only the base is judged here: the column already means "open",
    and draft is not collected, so a draft PR keeps its button and its refusal
    is reported by the failure bar. This is a static fact — collected PR base vs
    project config, no liveness and no queue state — which is why it is not the
    busy/idle precheck the claim arbitration removed (PR#178): the control plane
    stays the only arbiter at claim time. Unknown (unreadable config, uncollected
    base) reads as None, and every caller treats that as "in scope"."""
    base_ref = (pr.get('base_ref') or '').strip()
    if not cp_base or not base_ref:
        return None, ''
    if base_ref == cp_base:
        return True, ''
    return False, '控制面只评审 base=%s 的开放 PR（本 PR base=%s）' % (cp_base, base_ref)


def _reviewing_card(project, row, now):
    """One RUNNING reviewer run as a board card. Both sources feed this: the
    live store (``ex.id``) and the collected mirror (``ex.cp_id`` aliased to
    ``execution``) — the run-detail deep link consumes that id either way."""
    try:
        pr = json.loads(row['wi_payload'] or '{}').get('pr') or {}
    except ValueError:
        pr = {}
    number = pr.get('number')
    number = number if isinstance(number, int) and number > 0 else None
    expires = parse_ts(row['expires_at'])
    return {'card': 'exec', 'kind': 'reviewer',
            'org_repo': project['org_repo'], 'number': number,
            'title': pr.get('title') or '',
            'html_url': pr.get('html_url') or '',
            'key': row['wi_key'], 'worker': row['worker'],
            'execution': row['execution'],
            'branch': row['branch'], 'created_at': row['created_at'],
            'worktree': row['worktree'],
            'lease_expired': expires is not None and expires < now}


def _live_reviewing(db, pids, now):
    """One pass over the scope for the two facts the board reads from each
    project's machine-local control-plane store.

    Executions: each RUNNING reviewer run projects as a reviewing card keyed by
    its PR (payload.pr, as cp sync derives it as review:<n>); the PR's own card
    leaves review for as long as the execution runs. The live store is the
    arbiter — it is where claims actually land — and the collected mirror is the
    fallback for a project whose store cannot be read.

    Ledger: the newest review_round command per PR, i.e. what the last click on
    that card did, so the outcome survives a page reload instead of living in a
    four-second button label (the card shape itself is ``_trigger_card_state``).

    Returns ``(cards, cards_by_pr, triggers)``; ``triggers`` is keyed by
    ``(project id, PR number)`` because one pass spans every project."""
    cards, by_pr, triggers = [], {}, {}
    for pid, project in pids.items():
        live = _live_cp_view(project, now)
        if live is not None:
            triggers.update({(pid, number): row
                             for number, row in live['commands'].items()})
        rows = live['reviewing'] if live is not None else db.execute(
            "SELECT wi.key AS wi_key, wi.payload AS wi_payload, ex.cp_id AS execution, "
            "ex.* FROM executions ex JOIN work_items wi ON wi.project_id=ex.project_id "
            "AND wi.cp_id=ex.item_cp_id WHERE ex.project_id=? AND ex.state='RUNNING' "
            "AND wi.kind='reviewer'", (pid,))
        for row in rows:
            card = _reviewing_card(project, row, now)
            cards.append(card)
            if card['number'] is not None:
                by_pr[(pid, card['number'])] = card
    cards.sort(key=lambda c: parse_ts(c['created_at']) or 0)
    return cards, by_pr, triggers


def _trigger_card_state(trigger_row, verdict):
    """The trigger feedback riding on a pr card (spec 2026-09-18): the latest
    click's ledger state — a persistent failure badge, a running marker.
    ``None`` when there is no command or the card is already approved (a
    stale failure next to "ready to merge" is noise, not a call to act)."""
    if verdict == 'Approved for manual merge' or trigger_row is None:
        return None
    return {'state': trigger_row['state'],
            'fail_reason': trigger_row['fail_reason'],
            'at': trigger_row['created_at']}


def _repair_card_state(trigger_row, pr):
    """The repair twin: the newest ``repair_round`` click's ledger state on a
    conflicted PR card. Suppressed once the conflict is gone (mergeable flips
    truthy) and on mergeable-unknown, where there is no conflict to repair and
    so no button the failure could belong to. The collected column is an
    INTEGER/NULL, never a bool — judge by None / truthy, not identity."""
    mergeable = pr['mergeable']
    if trigger_row is None or mergeable is None or mergeable:
        return None
    return {'state': trigger_row['state'],
            'fail_reason': trigger_row['fail_reason'],
            'at': trigger_row['created_at']}


_REPAIR_KEY = 'repair:'


def _repair_number(row):
    """The PR number a handler row works on, from its item key — the repair
    payload carries a PR, but the key is the one fact both the live store and
    the collected mirror spell identically (``repair:<n>``)."""
    key = row['wi_key'] or ''
    if not key.startswith(_REPAIR_KEY):
        return None
    try:
        number = int(key[len(_REPAIR_KEY):])
    except ValueError:
        return None
    return number if number > 0 else None


def _repair_card(project, row, now):
    """One RUNNING repair run as a board card for 处理中 — same shape as the
    reviewer's execution card (the run-detail deep link consumes ``execution``
    either way), minus the PR title: the item key is the title."""
    expires = parse_ts(row['expires_at'])
    return {'card': 'exec', 'kind': 'handler',
            'org_repo': project['org_repo'], 'number': None, 'title': '',
            'issue_number': None, 'has_issue': False,
            'key': row['wi_key'], 'worker': row['worker'],
            'execution': row['execution'],
            'branch': row['branch'], 'created_at': row['created_at'],
            'worktree': row['worktree'],
            'lease_expired': expires is not None and expires < now}


def _live_repairing(db, pids, now):
    """One pass over the scope for the repair round's two facts — the twins
    of ``_live_reviewing``, read from the same machine-local stores.

    Executions: each RUNNING ``repair:<n>`` run projects as a 处理中 card the
    moment the claim lands (no collect-cycle wait); the collected mirror is
    the fallback for a project whose store cannot be read.

    Ledger: the newest ``repair_round`` command per PR, what the last click on
    a conflicted card did, surviving page reloads.

    Returns ``(cards, cards_by_pr, triggers)`` keyed like the reviewer's."""
    cards, by_pr, triggers = [], {}, {}
    for pid, project in pids.items():
        live = _live_cp_view(project, now)
        if live is not None:
            triggers.update({(pid, number): row
                             for number, row in live['repair_commands'].items()})
            rows = [row for row in live['running']
                    if (row['wi_key'] or '').startswith(_REPAIR_KEY)]
        else:
            rows = db.execute(
                "SELECT wi.key AS wi_key, wi.payload AS wi_payload, "
                "ex.cp_id AS execution, ex.* FROM executions ex "
                "JOIN work_items wi ON wi.project_id=ex.project_id "
                "AND wi.cp_id=ex.item_cp_id WHERE ex.project_id=? "
                "AND ex.state='RUNNING' AND wi.kind='handler' "
                "AND wi.key LIKE 'repair:%'", (pid,))
        for row in rows:
            card = _repair_card(project, row, now)
            cards.append(card)
            number = _repair_number(row)
            if number is not None:
                by_pr[(pid, number)] = card
    cards.sort(key=lambda c: parse_ts(c['created_at']) or 0)
    return cards, by_pr, triggers


def _running_issue_cards(db, pid, org_repo, now):
    """The development half of 处理中, from the collected mirror only: RUNNING
    handler executions that carry an issue (repair runs are projected live by
    ``_live_repairing`` — one owner per card kind, never a double show)."""
    cards = []
    for row in db.execute(
            "SELECT wi.payload AS wi_payload, wi.key AS wi_key, ex.* "
            "FROM executions ex JOIN work_items wi ON wi.project_id=ex.project_id "
            "AND wi.cp_id=ex.item_cp_id WHERE ex.project_id=? AND ex.state='RUNNING' "
            "AND wi.kind='handler' AND wi.key NOT LIKE 'repair:%'", (pid,)):
        try:
            issue = json.loads(row['wi_payload'] or '{}').get('issue') or {}
        except ValueError:
            issue = {}
        number = issue.get('number')
        number = number if isinstance(number, int) and number > 0 else None
        expires = parse_ts(row['expires_at'])
        card = {'card': 'exec', 'org_repo': org_repo, 'issue_number': number,
                'key': row['wi_key'], 'worker': row['worker'],
                'worktree': row['worktree'], 'branch': row['branch'],
                'created_at': row['created_at'],
                'lease_expired': expires is not None and expires < now}
        if number is not None:
            issue_row = db.execute('SELECT title FROM issues WHERE project_id=? AND number=?',
                                   (pid, number)).fetchone()
            card['title'] = issue_row['title'] if issue_row else ''
            card['has_issue'] = issue_row is not None
        else:
            card['has_issue'] = False
        cards.append(card)
    return cards


def kanban(db, org_repo=None, now=None):
    """Seven-column board of agent development work, derived from collected
    facts only. todo = open issues nobody picked up (a needs-human issue the
    user already answered returns here: the ball is with the handler);
    running = RUNNING handler executions; review = open PRs awaiting the
    reviewer (incl. changes requested, which returns here after rework);
    reviewing = live reviewer executions read straight from the project's
    machine-local control-plane store (a PR under active review moves here out
    of review until the execution finishes; the mirror is only the fallback
    when that store cannot be read); ready = approved, waiting for the manual
    merge; merged = recent merges (top MERGED_CAP across the scope);
    blocked = needs-human decisions, bad-verdict PRs, outbox limbo.
    Dedup: an issue appears in at most one of todo/running/blocked (RUNNING
    reference > needs-human > todo); an open PR appears in at most one of
    review/reviewing/ready/merged/blocked (live reviewer execution >
    verdict, exclusively; a just-merged PR can coexist with a still-RUNNING
    review execution until the next sync retires it). PR cards additionally
    carry what the manual trigger needs to be honest: ``trigger`` — the outcome
    of that PR's newest review_round command, read from the project's ledger —
    and ``review_scope`` / ``review_scope_note`` — whether the control plane
    can derive a review item for this PR at all. Pass org_repo to scope to one
    project (raises KeyError if unknown)."""
    now = now or 0
    projects = _project_map(db)
    pids = {p['id']: p for p in projects.values()
            if org_repo is None or p['org_repo'] == org_repo}
    if org_repo is not None and not pids:
        raise KeyError(org_repo)
    board = {column: [] for column in KANBAN_COLUMNS}
    reviewing_cards, reviewing_by_pr, triggers = _live_reviewing(db, pids, now)
    board['reviewing'].extend(reviewing_cards)
    repair_cards, repair_busy, repair_triggers = _live_repairing(db, pids, now)
    board['running'].extend(repair_cards)

    # PR-level cards: the latest verdict decides the column.
    linked_issues = set()
    for pid, project in pids.items():
        open_prs = _pr_rows(db, pid=pid, state='open')
        verdicts = verdicts_for(db, open_prs)
        cp_base = _cp_base(project)
        for pr in open_prs:
            for number in json.loads(pr['issue_numbers'] or '[]'):
                linked_issues.add((pid, number))
            if (pid, pr['number']) in reviewing_by_pr:
                continue  # under active review: the execution card owns the slot
            detail = latest_verdict_detail(db, pid, pr['number'],
                                           cp_verdicts=verdicts)
            verdict = detail['verdict'] if detail else None
            scope, scope_note = _review_scope(pr, cp_base)
            card = dict(pr, card='pr', org_repo=project['org_repo'], verdict=verdict,
                        verdict_at=detail['at'] if detail else None,
                        verdict_poster=detail['poster'] if detail else None,
                        verdict_body=detail['body'] if detail else None,
                        review_scope=scope, review_scope_note=scope_note)
            # The collected column is an INTEGER/NULL — hand the frontend a
            # real bool so JS strict equality says what it means (the 有冲突
            # chip predates this and only ever judged truthiness; the repair
            # button judges === false and would never fire on a raw 0).
            card['mergeable'] = None if pr['mergeable'] is None \
                else bool(pr['mergeable'])
            card['trigger'] = _trigger_card_state(
                triggers.get((pid, pr['number'])),
                                                  verdict)
            card['repair_trigger'] = _repair_card_state(
                repair_triggers.get((pid, pr['number'])), pr)
            busy = repair_busy.get((pid, pr['number']))
            card['repair_busy'] = busy is not None
            # 修复运行中的执行 id：PR 卡上「查看过程」内链直达运行详情，
            # 不必先去 处理中 列找那张执行卡。
            card['repair_exec'] = busy['execution'] if busy else None
            if verdict == 'Approved for manual merge':
                card['waiting_seconds'] = (max(0.0, now - detail['at'])
                                           if now and detail['at'] else None)
                board['ready'].append(card)
            elif verdict in ('Blocked', 'Validation failed'):
                board['blocked'].append(card)
            else:  # no verdict yet, or Changes requested (rework then re-review)
                board['review'].append(card)
    # The ready column doubles as the human action queue: longest wait on top.
    board['ready'].sort(key=lambda c: c.get('waiting_seconds') or 0, reverse=True)

    # Recent merges, capped across the whole scope (not per project).
    merged = []
    for pid, project in pids.items():
        for pr in _pr_rows(db, pid=pid, state='merged'):
            merged.append(dict(pr, card='pr', org_repo=project['org_repo'],
                               verdict=None))
    merged.sort(key=lambda r: parse_ts(r['merged_at']) or 0, reverse=True)
    board['merged'].extend(merged[:MERGED_CAP])

    # Issue-level cards: a RUNNING execution owns its issue first. Only
    # handler executions develop tasks — reviewer activity owns the reviewing
    # column, discover activity lives on the loops/events views, and repair
    # runs project live via _live_repairing (one owner per card kind).
    running_by_issue = {}
    for pid, project in pids.items():
        for card in _running_issue_cards(db, pid, project['org_repo'], now):
            if card['issue_number'] is not None:
                running_by_issue[(pid, card['issue_number'])] = card
            else:
                board['running'].append(card)
    board['running'].extend(running_by_issue.values())
    board['running'].sort(key=lambda c: parse_ts(c['created_at']) or 0)

    # A needs-human issue being actively reworked shows as running, not blocked.
    decisions = [d for d in pending_decisions(db, now=now)
                 if d['project_id'] in pids
                 and (d['project_id'], d['number']) not in running_by_issue]
    for issue in decisions:
        board['blocked'].append(dict(issue, card='decision'))
    blocked_issues = {(i['project_id'], i['number']) for i in decisions}

    for pid, project in pids.items():
        for row in db.execute("SELECT * FROM issues WHERE project_id=? AND state='open'",
                              (pid,)):
            key = (pid, row['number'])
            if key in running_by_issue or key in blocked_issues or key in linked_issues:
                continue
            board['todo'].append(dict(row, card='issue', org_repo=project['org_repo']))
    board['todo'].sort(key=lambda c: parse_ts(c['created_at']) or 0)

    for pid, project in pids.items():
        for row in db.execute("SELECT * FROM outbox WHERE project_id=? "
                              "AND state IN ('SENDING','UNKNOWN')", (pid,)):
            board['blocked'].append({'card': 'outbox', 'org_repo': project['org_repo'],
                                     'cp_id': row['cp_id'], 'action': row['action'],
                                     'state': row['state'],
                                     'created_at': row['created_at'],
                                     'title': 'outbox #%s %s' % (row['cp_id'], row['action'])})
    return board


def trends(db, days=14, now=None, org_repo=None):
    """Daily throughput buckets and cycle-time percentiles (hours). Buckets are
    LOCAL calendar days — the operator reads local dates, not UTC. Pass
    org_repo to scope everything to one project (raises KeyError if unknown)."""
    pid_filter = None
    if org_repo is not None:
        row = db.execute('SELECT id FROM projects WHERE org_repo=?',
                         (org_repo,)).fetchone()
        if row is None:
            raise KeyError(org_repo)
        pid_filter = row['id']
    now = now or 0
    daily = {}
    for offset in range(days - 1, -1, -1):
        date = datetime.fromtimestamp(now - offset * 86400).date().isoformat()
        daily[date] = {'date': date, 'issues_created': 0, 'issues_closed': 0,
                       'prs_created': 0, 'prs_merged': 0}
    first = now - days * 86400

    def bucket(moment):
        return datetime.fromtimestamp(moment).date().isoformat()

    issue_query = 'SELECT * FROM issues' + (' WHERE project_id=?' if pid_filter else '')
    issue_params = (pid_filter,) if pid_filter else ()
    issue_to_pr, pr_to_approved = [], []
    verdicts = {}   # lazy per-project _cp_verdicts map (one scan per project)
    for row in db.execute(issue_query, issue_params):
        created = parse_ts(row['created_at'])
        if created and created >= first:
            key = bucket(created)
            if key in daily:
                daily[key]['issues_created'] += 1
        closed = parse_ts(row['closed_at'])
        if closed and closed >= first:
            key = bucket(closed)
            if key in daily:
                daily[key]['issues_closed'] += 1
    for pr in _pr_rows(db, pid=pid_filter, state='all'):
        created = parse_ts(pr['created_at'])
        pid = pr['project_id']
        if pid not in verdicts:   # setdefault would still evaluate per PR
            verdicts[pid] = _verdicts._cp_verdicts(db, pid)
        if created and created >= first:
            key = bucket(created)
            if key in daily:
                daily[key]['prs_created'] += 1
        if pr['merged']:
            merged = parse_ts(pr['merged_at'])
            if merged and merged >= first:
                key = bucket(merged)
                if key in daily:
                    daily[key]['prs_merged'] += 1
        verdict = latest_verdict(db, pid, pr['number'], cp_verdicts=verdicts[pid])
        if created and verdict and verdict[0] == 'Approved for manual merge':
            pr_to_approved.append((verdict[1] - created) / 3600.0)
        if created and pr['issue_numbers']:
            for number in json.loads(pr['issue_numbers'] or '[]'):
                issue = db.execute('SELECT created_at FROM issues WHERE project_id=? AND number=?',
                                   (pr['project_id'], number)).fetchone()
                if issue:
                    start = parse_ts(issue['created_at'])
                    if start and created >= start:
                        issue_to_pr.append((created - start) / 3600.0)

    def pct(values, share):
        if not values:
            return None
        ordered = sorted(values)
        index = max(0, min(len(ordered) - 1, int(len(ordered) * share + 0.999) - 1))
        return round(ordered[index], 2)

    return {'daily': list(daily.values()),
            'cycle': {'issue_to_pr': {'p50': pct(issue_to_pr, 0.5),
                                      'p90': pct(issue_to_pr, 0.9),
                                      'n': len(issue_to_pr)},
                      'pr_to_approved': {'p50': pct(pr_to_approved, 0.5),
                                         'p90': pct(pr_to_approved, 0.9),
                                         'n': len(pr_to_approved)}}}
