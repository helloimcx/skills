"""Execution-history payloads (spec docs/specs/2026-09-21-dash-execution-history.md).

Two jobs, both read-only, no platform write:

- ``history`` — the unified list behind GET /api/history: loop executions from
  the collected mirror (agent resolved from the command-ledger mirror) merged
  with the derived subagent archives (``agent_runs``), one reverse-chronological
  feed with project/source/state/agent/q filters. The response carries the
  distinct ``agents`` facet (computed without the agent filter) so the filter
  dropdown stays data-driven — a new agent needs no frontend change.
- ``agent_run_detail`` — the run view's payload for an agent-run id (the
  ``platform:session`` form of GET /api/run's ``exec``): the archive row
  projected into the same execution/session/timeline/evidence shape the loop
  rounds already render, with the stream bound as ``agent-session``.
"""
import json
import sqlite3

from . import agentruns
from .schema import parse_ts

LOOP_LIMIT = 500     # mirror scan ceilings before filters (one page serves ≤200)
AGENT_LIMIT = 500
DEFAULT_LIMIT = 200

TITLE_HEAD_CAP = 160

# Subagent sessions record their working directory, which is often a git
# worktree (…\.git-worktrees\handler-179, …\.claude\worktrees\…): the project
# facet collapses those to the repository root and matches case-insensitively
# (Windows filesystems make D:\ vs d:\ the same project).
WORKTREE_MARKERS = ('\\.git-worktrees\\', '\\.claude\\worktrees\\',
                    '\\.agents\\', '\\.agent-locks\\', '\\worktrees\\')


def _norm_project(path):
    path = str(path or '').replace('/', '\\')
    lowered = path.lower()
    for marker in WORKTREE_MARKERS:
        at = lowered.find(marker)
        if at > 0:
            return path[:at]
    return path


def _project_key(path):
    return _norm_project(path).casefold()


def _title_of(key, payload):
    """(title, number) from a mirrored work item — the same shape the
    run view's item projection uses."""
    item = {'title': '', 'number': None}
    try:
        pr = json.loads(payload or '{}').get('pr') or {}
    except ValueError:
        pr = {}
    number = pr.get('number')
    if isinstance(number, int) and not isinstance(number, bool):
        item['number'] = number
    item['title'] = _head(pr.get('title') or key or '', TITLE_HEAD_CAP)
    return item


def _head(text, cap):
    text = ' '.join(str(text or '').split())
    return text[:cap] + '…' if len(text) > cap else text


def _loop_rows(db):
    """Mirror executions → unified list rows (agent from the latest ledger
    command; unknown ledger → the registered-command default, claude)."""
    rows = db.execute('''
        SELECT ex.cp_id, ex.state, ex.created_at, pr.org_repo,
               wi.key AS wi_key, wi.kind AS wi_kind, wi.payload AS wi_payload,
               cmd.agent AS cmd_agent
        FROM executions ex
        JOIN projects pr ON pr.id = ex.project_id
        LEFT JOIN work_items wi
            ON wi.project_id = ex.project_id AND wi.cp_id = ex.item_cp_id
        LEFT JOIN commands cmd ON cmd.id = (
            SELECT MAX(c2.id) FROM commands c2
            WHERE c2.project_id = ex.project_id AND c2.execution = ex.cp_id)
        ORDER BY ex.created_at DESC LIMIT ?''', (LOOP_LIMIT,)).fetchall()
    out = []
    for row in rows:
        item = _title_of(row['wi_key'], row['wi_payload'])
        key = str(row['wi_key'] or '')
        loop = key.split(':', 1)[0] if key else (row['wi_kind'] or '')
        started = parse_ts(row['created_at'])
        out.append({
            'id': str(row['cp_id']), 'kind': 'loop', 'loop': loop,
            'platform': '', 'project': row['org_repo'], 'org_repo': row['org_repo'],
            'title': item['title'] or key, 'state': row['state'] or '',
            'agent': row['cmd_agent'] or 'claude',
            'started_at': started, 'duration_s': None})
    return out


def _agent_summary(prompt):
    """Structured task summary from a subagent prompt, instead of the raw
    prompt head (issue #327). Extracts loop kind / round / PR target when the
    prompt follows the code-loop template; falls back to the prompt head."""
    import re
    text = ' '.join(str(prompt or '').split())
    parts = []
    m = re.search(r'code-loop\s+(\S+)\s+循环', text)
    if m:
        parts.append(m.group(1))
    m = re.search(r'第\s*(\d+)\s*轮', text)
    if m:
        parts.append('第 %s 轮' % m.group(1))
    if re.search(r'评审|Review', text, re.IGNORECASE):
        parts.append('评审')
    elif re.search(r'开发|实现|Implement', text, re.IGNORECASE):
        parts.append('开发')
    elif re.search(r'巡检|Inspect', text, re.IGNORECASE):
        parts.append('巡检')
    m = re.search(r'PR\s*#\s*(\d+)', text, re.IGNORECASE)
    if m:
        parts.append('PR #%s' % m.group(1))
    if parts:
        return ' · '.join(parts)
    return _head(text, TITLE_HEAD_CAP)


def _agent_rows(db):
    rows = db.execute(
        'SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT ?',
        (AGENT_LIMIT,)).fetchall()
    out = []
    for row in rows:
        started = row['started_at']
        out.append({
            'id': row['id'], 'kind': 'agent', 'loop': '', 'platform': row['platform'],
            'project': _norm_project(row['project'] or row['cwd']), 'org_repo': '',
            'parent_ref': row['parent_ref'] or '',
            'title': _agent_summary(row['task_prompt']) or row['id'],
            'state': row['state'] or '', 'agent': row['platform'],
            'started_at': started, 'duration_s': row['duration_s']})
    return out


def _match(row, filters):
    if filters['source'] != 'all' and row['kind'] != filters['source']:
        return False
    if filters['state'] and row['state'] != filters['state']:
        return False
    if filters['agent'] and row['agent'] != filters['agent']:
        return False
    if filters['project'] and filters['project'].casefold() not in (
            row['project'].casefold(), row['org_repo'].casefold()):
        return False
    if filters['q']:
        needle = filters['q'].lower()
        haystack = ' '.join(filter(None, (row['title'], row['loop'], row['id']))).lower()
        if needle not in haystack:
            return False
    return True


def _facets(merged, filters):
    """Distinct agents/projects for the filter dropdowns, computed WITHOUT
    the agent/project filters so option lists stay data-driven and stable
    while a filter is selected."""
    facet_view = dict(filters, agent='', project='')
    loose = [r for r in merged if _match(r, facet_view)]
    agents = sorted({r['agent'] for r in loose if r['agent']})
    seen_projects = {}
    for r in loose:
        display = r['org_repo'] or r['project']
        if not display:
            continue
        seen_projects.setdefault(_project_key(display), display)
    projects = sorted(seen_projects.values(), key=str.casefold)
    return agents, projects


def history(db, query):
    """GET /api/history — the merged, filtered, reverse-chronological feed."""
    filters = {'source': query.get('source') or 'all',
               'state': (query.get('state') or '').upper(),
               'agent': query.get('agent') or '',
               'project': query.get('project') or '',
               'q': (query.get('q') or '').strip()}
    try:
        limit = max(1, min(500, int(query.get('limit', DEFAULT_LIMIT))))
        offset = max(0, int(query.get('offset', 0)))
    except ValueError:
        limit, offset = DEFAULT_LIMIT, 0
    loop_rows = _loop_rows(db) if filters['source'] in ('all', 'loop') else []
    agent_rows = _agent_rows(db) if filters['source'] in ('all', 'agent') else []
    merged = sorted(loop_rows + agent_rows,
                    key=lambda r: r['started_at'] or 0, reverse=True)
    agents, projects = _facets(merged, filters)
    rows = [r for r in merged if _match(r, filters)]
    return {'total': len(rows), 'agents': agents, 'projects': projects,
            'rows': rows[offset:offset + limit]}


def agent_run_detail(db, exec_id, since):
    """GET /api/run's payload for an agent-run id, or None (→ 404). The
    archive row projects into the loop round's payload shape; the stream
    dispatches to the platform adapter (bound='agent-session' or the unbound
    placeholder when that platform has no reader)."""
    row = db.execute('SELECT * FROM agent_runs WHERE id=?', (exec_id,)).fetchone()
    if row is None:
        return None
    run = dict(row)
    state = run['state'] or 'SUCCEEDED'
    execution = {
        'id': run['id'], 'state': state, 'source': 'agent',
        'platform': run['platform'], 'project': run['project'] or run['cwd'] or '',
        'parent_ref': run['parent_ref'], 'duration_s': run['duration_s'],
        'worker': None, 'branch': None, 'worktree': None, 'result': run['result_head'],
        'created_at': run['started_at'], 'expires_at': None, 'ended_at': run['ended_at']}
    item = {'key': run['id'], 'number': None,
            'title': _head(run['task_prompt'], TITLE_HEAD_CAP) or run['id']}
    try:
        session = agentruns.session_events(run, since)
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        # a broken stream degrades to the unbound placeholder (timeline
        # only) — the run view never 500s on a hostile session store
        since_i = int(since or 0)
        session = {'bound': None, 'path': None, 'events': [],
                   'offset': since_i, 'size': since_i, 'skipped': 0}
    started = run['started_at']
    timeline = [
        {'kind': 'agentrun.start', 'at': started,
         'payload': json.dumps({'platform': run['platform'],
                                'parent': run['parent_ref']}, ensure_ascii=False)},
        {'kind': 'agentrun.end', 'at': run['ended_at'],
         'payload': json.dumps({'state': state, 'duration_s': run['duration_s']},
                               ensure_ascii=False)},
        {'kind': 'agentrun.collected', 'at': run['first_seen_at'],
         'payload': 'agent_runs upsert'}]
    evidence = [
        {'kind': '原始凭证', 'at': started, 'payload': run['transcript_ref'] or ''},
        {'kind': '任务提示词', 'at': started,
         'payload': _head(run['task_prompt'], 300)}]
    if run['parent_ref']:
        evidence.insert(1, {'kind': '父会话', 'at': started,
                            'payload': run['parent_ref']})
    return {'execution': execution, 'item': item, 'session': session,
            'timeline': timeline, 'evidence': evidence}
