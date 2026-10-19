"""Dashboard store health layer: loop health, worker rollup, events, alerts, overview.

Aggregations over the other layers; depends on views and ingest, never the
reverse.
"""
from datetime import datetime

from .ingest import list_machines
from .schema import ALERT_DEFAULTS, parse_ts
from .views import (_project_map, exceptions as _exceptions, funnel,
                    pending_decisions, pending_merges)


LOOP_KINDS = ('discover', 'handler', 'reviewer')


def _ready_since_map(db):
    """Oldest READY item per (project, kind): proof of work waiting for a
    loop. _stall_alerts uses it to exempt handler/reviewer loops whose queue
    is empty — bare silence there is compliant idle (claim null), not a
    stall."""
    ready_since = {}
    for row in db.execute(
            "SELECT project_id, kind, created_at FROM work_items WHERE state='READY'"):
        key = (row['project_id'], row['kind'])
        moment = parse_ts(row['created_at'])
        if moment is None:
            continue
        if key not in ready_since or moment < ready_since[key]:
            ready_since[key] = moment
    return ready_since


def loop_health(db, now=None):
    """Per project × loop kind: last execution activity and running items.
    Enabled projects get the full discover/handler/reviewer matrix even when
    the control plane has no work_items yet — a missing card reads as a blind
    spot, and last_activity=None never triggers the stall alert (no data is
    not evidence of a stall).

    Last activity comes from two grouped scans rather than the former
    LEFT JOIN … MAX(COALESCE(ex.created_at, wi.created_at)) over every
    work_items row (~2s of SQL on the live store; the join also compared the
    timestamp text, so an older ISO string could outsort a newer epoch-text
    one). Rows are compared here by parsed epoch — the same rule
    worker_summary uses — so the newest fact wins regardless of which format
    its writer chose."""
    last_by_key = {}

    def touch(key, raw):
        if raw is None:
            return
        known = last_by_key.get(key)
        if known is None or (parse_ts(raw) or 0) > (parse_ts(known) or 0):
            last_by_key[key] = raw

    # idx_wi_cover makes the item-side scan index-only (no payload rows read).
    for row in db.execute('SELECT project_id, kind, created_at FROM work_items'):
        touch((row['project_id'], row['kind']), row['created_at'])
    for row in db.execute(
            'SELECT ex.project_id AS project_id, wi.kind AS kind, '
            'ex.created_at AS created_at FROM executions ex '
            'JOIN work_items wi ON wi.project_id=ex.project_id '
            'AND wi.cp_id=ex.item_cp_id'):
        touch((row['project_id'], row['kind']), row['created_at'])
    running = {(r['project_id'], r['kind']): r['n'] for r in db.execute(
        "SELECT project_id, kind, COUNT(*) AS n FROM work_items WHERE state='RUNNING' "
        'GROUP BY project_id, kind')}
    ready_since = _ready_since_map(db)
    projects = _project_map(db)

    def org_of(pid):
        row = projects.get(pid)
        return row['org_repo'] if row is not None else ''

    keys = set(last_by_key)
    for project in projects.values():
        if project['enabled']:
            keys.update((project['id'], kind) for kind in LOOP_KINDS)
    health = []
    for pid, kind in sorted(keys, key=lambda k: (org_of(k[0]), k[1] or '')):
        last = parse_ts(last_by_key.get((pid, kind)))
        health.append({'org_repo': org_of(pid), 'kind': kind,
                       'last_activity': last,
                       'age_seconds': max(0.0, (now or 0) - last) if (now and last) else None,
                       'running': running.get((pid, kind), 0),
                       'ready_since': ready_since.get((pid, kind))})
    return health


def worker_summary(db, now=None, config=None, days=7):
    """Per-machine worker rollup: registry inventory joined with what each
    machine actually did, from the execution mirror alone.

    The CP server stamps remote worker ids as `<machine>/<suffix>`, so the
    machine is the part before the first slash (a bare worker string is kept
    as-is — pre-stamping history and in-process local runs surface as
    unregistered machines, shown for context, never alarmed). Recency math
    happens in Python via parse_ts because created_at mixes epoch text and
    ISO-8601 (SQL MAX would compare the textual forms).

    A machine × loop kind reads as 应动未动 (stalled) when its newest claim is
    older than the kind's threshold (worker_stall_seconds) — and, for
    handler/reviewer, only when READY work was already waiting before the
    silence began: those loops claim nothing (and leave no execution trace)
    on an empty-queue round, so bare silence there is legitimate idle.
    discover is schedule-driven and must run even when it would find
    nothing, so bare silence flags it. Expectation derives from observed
    history — a kind the machine never claimed, and any machine that is
    unregistered, operator-disabled or revoked, can never stall, so
    brand-new, retired or deliberately stopped machines don't produce false
    alarms. Only executions whose work_items row still joins carry kind
    data; rows without one still count for outcomes."""
    now = now or 0
    cfg = dict(ALERT_DEFAULTS)
    cfg.update(config or {})
    thresholds = cfg['worker_stall_seconds']
    window_start = now - days * 86400

    machines = {}
    for row in list_machines(db):
        machines[row['name']] = dict(row, registered=True, kinds={}, running=[],
                                     outcomes={}, stalled_kinds=[], last_claim=None)

    def entry_for(name):
        if name not in machines:
            machines[name] = {'name': name, 'id': None, 'enabled': 1, 'revoked': 0,
                              'created_at': None, 'last_seen': None, 'last_addr': None,
                              'agent_version': '', 'projects': '', 'registered': False,
                              'kinds': {}, 'running': [], 'outcomes': {},
                              'stalled_kinds': [], 'last_claim': None}
        return machines[name]

    # Oldest READY item per (project, kind): proof of work that was claimable
    # at a given time (created_at mixes epoch text and ISO, so parse in Python).
    ready_waiting = {}
    for row in db.execute(
            "SELECT wi.kind, wi.created_at, pr.org_repo FROM work_items wi "
            "JOIN projects pr ON pr.id=wi.project_id WHERE wi.state='READY'"):
        moment = parse_ts(row['created_at'])
        if not row['kind'] or moment is None:
            continue
        key = (row['org_repo'], row['kind'])
        if key not in ready_waiting or moment < ready_waiting[key]:
            ready_waiting[key] = moment

    # (machine, kind) -> projects the machine itself claimed that kind in;
    # waiting work only implicates the machine on projects it actually runs.
    claimed = {}

    rows = db.execute(
        'SELECT ex.worker, ex.state, ex.created_at, ex.expires_at, ex.worktree, '
        'ex.branch, wi.kind, wi.key, pr.org_repo FROM executions ex '
        'LEFT JOIN work_items wi ON wi.project_id=ex.project_id '
        'AND wi.cp_id=ex.item_cp_id '
        'JOIN projects pr ON pr.id=ex.project_id')
    for row in rows:
        worker = row['worker'] or ''
        entry = entry_for(worker.split('/', 1)[0] if worker else '?')
        created = parse_ts(row['created_at'])
        kind = row['kind']
        if kind:
            slot = entry['kinds'].get(kind)
            if slot is None:
                slot = entry['kinds'][kind] = {'last_claim': None, 'age_seconds': None,
                                               'stalled': False, 'total': 0}
            slot['total'] += 1
            if created is not None and (slot['last_claim'] is None
                                        or created > slot['last_claim']):
                slot['last_claim'] = created
            claimed.setdefault((entry['name'], kind), set()).add(row['org_repo'])
        state = row['state'] or ''
        if state == 'RUNNING':
            expires = parse_ts(row['expires_at'])
            entry['running'].append({
                'org_repo': row['org_repo'], 'kind': kind, 'key': row['key'],
                'created_at': created, 'expires_at': expires,
                'lease_expired': expires is not None and expires < now,
                'worktree': row['worktree'], 'branch': row['branch']})
        if created is not None and created >= window_start:
            entry['outcomes'][state] = entry['outcomes'].get(state, 0) + 1
        if created is not None and (entry['last_claim'] is None
                                    or created > entry['last_claim']):
            entry['last_claim'] = created

    summary = []
    for entry in machines.values():
        # Only enrolled, enabled machines can stall: an operator-disabled or
        # revoked machine stops claiming by design, unregistered ones are
        # pre-stamping history — neither may ever produce a stall alarm.
        monitorable = (entry['registered'] and entry['enabled'] and not entry['revoked'])
        for kind, slot in entry['kinds'].items():
            threshold = thresholds.get(kind)
            if threshold is None or slot['last_claim'] is None:
                continue
            slot['age_seconds'] = max(0.0, now - slot['last_claim'])
            stalled = slot['age_seconds'] > threshold
            if stalled and kind != 'discover':
                waiting = [ready_waiting.get((org, kind))
                           for org in claimed.get((entry['name'], kind), ())]
                stalled = any(t is not None and t < slot['last_claim']
                              for t in waiting)
            slot['stalled'] = monitorable and stalled
            if slot['stalled']:
                entry['stalled_kinds'].append(kind)
        entry['stalled_kinds'].sort()
        entry['running'].sort(key=lambda r: r['created_at'] or 0)
        summary.append(entry)

    def order(entry):
        live = entry['registered'] and entry['enabled'] and not entry['revoked']
        seen = entry['last_seen'] or entry['last_claim'] or 0
        return (0 if live else 1, 0 if entry['registered'] else 1, -seen, entry['name'])

    summary.sort(key=order)
    return {'generated_at': now, 'window_days': days,
            'thresholds': thresholds, 'machines': summary}


def events(db, org_repo=None, days=7, now=None, kind=None, limit=300):
    """Control-plane event timeline: daily counts over the window plus the most
    recent rows. cp_events are already collected, so this is a pure read. The
    window is the last `days` LOCAL calendar days, matching the buckets.
    created_at mixes epoch text and ISO, so window filtering must stay in
    Python: the daily scan walks one project's cp_events (tens of ms at ~10^4
    rows); the recent list uses SQL ordering and just under-fills on quiet
    tails instead of scanning."""
    now = now or 0
    first_day = datetime.fromtimestamp(now - (days - 1) * 86400).date()
    first = datetime(first_day.year, first_day.month, first_day.day).timestamp()
    projects = {pid: p for pid, p in _project_map(db).items()
                if org_repo is None or p['org_repo'] == org_repo}
    daily = {}
    for offset in range(days - 1, -1, -1):
        date = datetime.fromtimestamp(now - offset * 86400).date().isoformat()
        daily[date] = 0
    recent = []
    for pid, project in projects.items():
        base = 'SELECT kind, payload, created_at FROM cp_events WHERE project_id=?'
        params = [pid]
        if kind:
            base += ' AND kind=?'
            params.append(kind)
        for row in db.execute(base, params):
            moment = parse_ts(row['created_at'])
            if moment is None or moment < first or moment > now:
                continue
            key = datetime.fromtimestamp(moment).date().isoformat()
            if key in daily:
                daily[key] += 1
        for row in db.execute(base + ' ORDER BY cp_id DESC LIMIT ?', params + [limit]):
            moment = parse_ts(row['created_at'])
            if moment is None or moment < first or moment > now:
                continue
            recent.append({'org_repo': project['org_repo'], 'kind': row['kind'],
                           'created_at': row['created_at'], 'epoch': moment,
                           'payload': row['payload']})
    recent.sort(key=lambda e: e['epoch'], reverse=True)
    return {'total': sum(daily.values()),
            'daily': [{'date': date, 'count': daily[date]} for date in sorted(daily)],
            'recent': recent[:limit]}


def _aging_alerts(found, cfg, decisions):
    """needs_human_aging: open decisions waiting on the user past the budget.
    Each decision row already carries org_repo, so no project join is needed."""
    for issue in decisions:
        if issue.get('waiting_seconds') and issue['waiting_seconds'] > cfg['needs_human_aging_seconds']:
            secs = issue['waiting_seconds']
            # 时长 ≥48h 用「天」才符合直觉（issue #324）：376 小时 = 15.7 天
            if secs >= 48 * 3600:
                msg = 'Issue #%d 等人工决策已超 %.1f 天' % (issue['number'], secs / 86400.0)
            else:
                msg = 'Issue #%d 等人工决策已超 %.0f 小时' % (issue['number'], secs / 3600.0)
            found.append({'severity': 'warn', 'rule': 'needs_human_aging',
                          'org_repo': issue['org_repo'],
                          'key': 'issue-%d' % issue['number'],
                          'message': msg})


def _exception_alerts(found, items):
    """outbox limbo / dead leases / blocked PRs, one alert per exception."""
    for item in items:
        rule = {'outbox': 'outbox_limbo', 'lease_expired': 'lease_expired',
                'pr': 'pr_' + item.get('verdict', 'blocked').lower().replace(' ', '_')}[item['type']]
        found.append({'severity': item['severity'], 'rule': rule,
                      'org_repo': item['org_repo'], 'key': item['key'],
                      'message': item['message']})


def _stall_alerts(found, stall, loop_rows):
    """loop_stall: a loop kind silent past its threshold. handler/reviewer
    loops claim nothing on an empty-queue round — bare silence there is
    compliant idle, not a stall (the same exemption worker_summary applies
    to _worker_stall_alerts). Only report when READY work is waiting for
    the loop; discover is schedule-driven and must run even when it would
    find nothing, so bare silence flags it."""
    for row in loop_rows:
        if row['age_seconds'] is None or row['age_seconds'] <= stall:
            continue
        if row['kind'] != 'discover' and row.get('ready_since') is None:
            continue
        found.append({'severity': 'warn', 'rule': 'loop_stall',
                      'org_repo': row['org_repo'],
                      'key': 'loop-' + row['kind'],
                      'message': '%s loop 已 %.1f 小时无活动（超过停摆阈值）'
                                 % (row['kind'], row['age_seconds'] / 3600.0)})


def _worker_stall_alerts(found, machines):
    """worker_stall: a machine × kind that should have claimed and did not."""
    for machine in machines:
        for kind in machine['stalled_kinds']:
            age = machine['kinds'][kind]['age_seconds']
            found.append({'severity': 'warn', 'rule': 'worker_stall',
                          'org_repo': '', 'key': 'worker-%s-%s' % (machine['name'], kind),
                          'message': '机器 %s 的 %s loop 已 %.1f 小时未领取工作项（应动未动，调度可能已停）'
                                     % (machine['name'], kind, age / 3600.0)})


def alerts(db, now=None, config=None, decisions=None, exceptions=None,
           loops=None):
    """Rule-based alert list; config overrides ALERT_DEFAULTS. Each alert
    carries a stable `key` (rule-scoped entity id) so push channels can
    deduplicate without re-pinging on every evaluation. overview passes the
    sections it already derived (``decisions`` / ``exceptions`` / ``loops``)
    so one render computes each of them exactly once; standalone callers
    (notify) omit them and each section is derived here as before. The
    ``exceptions`` parameter shadows the imported helper, which stays
    reachable as ``_exceptions`` for the derive-here branch."""
    cfg = dict(ALERT_DEFAULTS)
    cfg.update(config or {})
    found = []

    def add(severity, rule, org_repo, message, key=''):
        found.append({'severity': severity, 'rule': rule, 'org_repo': org_repo,
                      'key': key, 'message': message})

    for project in _project_map(db).values():
        org = project['org_repo']
        if not project['enabled']:
            add('info', 'machine_disabled', org,
                '项目已配置但本机未 enable（写入命令将返回 MACHINE_NOT_ENABLED）', key='enable')
        if project['enabled'] and project['last_ok_collect_at'] is None:
            add('warn', 'collect_never_ok', org, '已启用项目尚无一次成功采集', key='collect')
        ok_at = project['last_ok_collect_at']
        if ok_at is not None and now and now - ok_at > cfg['collect_interval'] * cfg['collect_stale_factor']:
            add('warn', 'collect_stale', org,
                '采集已 %.1f 小时未成功' % ((now - ok_at) / 3600.0), key='collect')
    now = now or 0
    if decisions is None:
        decisions = pending_decisions(db, now=now)
    _aging_alerts(found, cfg, decisions)
    if exceptions is None:
        exceptions = _exceptions(db, now=now)
    _exception_alerts(found, exceptions)
    if loops is None:
        loops = loop_health(db, now=now)
    _stall_alerts(found, cfg['loop_stall_seconds'], loops)
    _worker_stall_alerts(found, worker_summary(db, now=now, config=config)['machines'])
    severity_order = {'error': 0, 'warn': 1, 'info': 2}
    found.sort(key=lambda a: (severity_order[a['severity']], a['org_repo'], a['rule']))
    return found


def overview(db, now=None, config=None):
    """Everything the dashboard header/project cards need, in one call. Each
    derived section (exceptions / merges / decisions / loop health) is
    computed exactly once and shared between the return value and alerts."""
    now = now or 0
    exc_items = _exceptions(db, now=now)
    exception_counts = {}
    for item in exc_items:
        exception_counts[item['org_repo']] = exception_counts.get(item['org_repo'], 0) + 1
    merges = pending_merges(db, now=now)
    decisions = pending_decisions(db, now=now)
    loops = loop_health(db, now=now)
    projects = []
    for project in _project_map(db).values():
        pid = project['id']
        open_issues = db.execute("SELECT COUNT(*) FROM issues WHERE project_id=? AND state='open'",
                                 (pid,)).fetchone()[0]
        open_prs = db.execute("SELECT COUNT(*) FROM prs WHERE project_id=? AND state='open' AND merged=0",
                              (pid,)).fetchone()[0]
        entry = dict(project)
        entry['counts'] = {
            'open_issues': open_issues, 'open_prs': open_prs,
            'pending_merges': len([m for m in merges if m['project_id'] == pid]),
            'pending_decisions': len([d for d in decisions if d['project_id'] == pid]),
            'exceptions': exception_counts.get(project['org_repo'], 0),
        }
        projects.append(entry)
    cfg = dict(ALERT_DEFAULTS)
    cfg.update(config or {})
    return {'generated_at': now,
            'machines': list_machines(db),
            'collected_at': max([p['last_ok_collect_at'] or 0 for p in projects],
                                default=None) or None,
            'projects': projects,
            'funnels': {p['org_repo']: funnel(db, p['id']) for p in projects},
            'config': cfg,
            'alerts': alerts(db, now=now, config=config, decisions=decisions,
                             exceptions=exc_items, loops=loops),
            'loops': loops}
