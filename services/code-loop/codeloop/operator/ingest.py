"""Operator ingest gate: the single decision point every signal passes.

Whitelist (alert rules / event kinds) → self-exclusion → same-signal active
dedup → cooldown → daily cap → freshness. Fired signals become operator work
items; every decision (including skips) lands in the decision log
(~/.code-loop/logs/operator-log.jsonl, trigger-log discipline). The cap's
degrade contract: beyond it no new rounds spawn, while the plain webhook
alert push upstream is untouched — humans stay informed, tokens stay bound.
"""
import json
import time
from pathlib import Path

from . import store as op_store

SELF_EVENT_PREFIX = 'operator.'
# Watchdog revival lines we ingest (only these carry a decision worth keeping).
WATCHDOG_ACTIONS = ('spawn',)


def _local_date(now):
    return time.strftime('%Y-%m-%d', time.localtime(now))


def _meta_get(store, key, default=None):
    row = store.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row['value'] if row is not None else default


def _meta_set(store, key, value):
    store.db.execute('INSERT INTO meta (key,value) VALUES (?,?) '
                     'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))


def _log(store, home, entry):
    entry['logged_at'] = time.time()
    try:
        path = op_store.log_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except OSError:
        pass  # the ledger never blocks the gate


def _decision(store, home, *, decision, source, kind, key, reason=None,
              item=None, **extra):
    out = dict(decision=decision, source=source, kind=kind, key=key)
    if reason:
        out['reason'] = reason
    if item is not None:
        out['item'] = item
    out.update(extra)
    _log(store, home, out)
    return out


def alert_allowed(cfg, rule):
    return rule in cfg['alert_rules']


def event_allowed(cfg, kind):
    if kind.startswith(SELF_EVENT_PREFIX):
        return False
    return kind in cfg['event_kinds'] or kind.startswith('exam.')


def allowed(cfg, source, kind):
    if source == 'alert':
        return alert_allowed(cfg, kind)
    if source == 'watchdog':
        return kind == 'watchdog_spawn'
    if source == 'manual':
        return kind == 'manual'
    return event_allowed(cfg, kind)


def check_and_record(store, cfg, *, source, kind, key, payload, org_repo='',
                     severity='', now=None, fresh=True, home=None, manual=False):
    """Run one signal through the gate; fired → operator work item + command.
    manual=True (dashboard trigger) bypasses cooldown and the daily cap —
    human intent — but keeps the same-signal single-live-round dedup."""
    now = time.time() if now is None else now
    if kind.startswith(SELF_EVENT_PREFIX):
        return _decision(store, home, decision='skipped_self', source=source,
                         kind=kind, key=key, reason='operator events are never self-consumed')
    if not allowed(cfg, source, kind):
        return _decision(store, home, decision='skipped_rule', source=source,
                         kind=kind, key=key,
                         reason='rule not whitelisted' if source == 'alert'
                         else 'kind not watched')
    sig = '%s|%s' % (source, kind + ':' + key)
    last = _meta_get(store, 'operator_last:' + sig)
    if not manual and last is not None and now - float(last) < cfg['cooldown_minutes'] * 60:
        return _decision(store, home, decision='skipped_cooldown', source=source,
                         kind=kind, key=key)
    if op_store.active_signal(store, source, key) is not None:
        return _decision(store, home, decision='skipped_active', source=source,
                         kind=kind, key=key, reason='a round is still live')
    today = _local_date(now)
    fired_today = int(_meta_get(store, 'operator_cap:' + today, '0'))
    if not manual and fired_today >= cfg['max_rounds_per_day']:
        return _decision(store, home, decision='skipped_cap', source=source,
                         kind=kind, key=key, degrade_webhook=True,
                         reason='daily cap reached')
    if not fresh:
        return _decision(store, home, decision='stale_noop', source=source,
                         kind=kind, key=key, reason='signal no longer present')
    item, _created = op_store.put_signal(store, source=source, kind=kind, key=key,
                                         payload=payload, org_repo=org_repo,
                                         severity=severity, now=now)
    row = store.db.execute('SELECT rowid FROM work_items WHERE id=?',
                           (item['id'],)).fetchone()
    _enqueue_round(store, cfg, source, payload, row['rowid'])
    _meta_set(store, 'operator_last:' + sig, now)
    _meta_set(store, 'operator_cap:' + today, fired_today + 1)
    return _decision(store, home, decision='fired', source=source, kind=kind,
                     key=key, item=item)


def _enqueue_round(store, cfg, source, payload, number):
    """operator_round command carrying the round's spawn options. The manual
    trigger (dashboard dialog) records its agent/model picks on the payload —
    they must reach the command row or the round silently falls back to the
    operator config defaults (review MAJOR, Spec AC6). Other sources keep cfg
    defaults."""
    payload = payload or {}
    agent = payload.get('agent') if source == 'manual' else None
    model = payload.get('model') if source == 'manual' else None
    store.enqueue_command('operator_round', number=number,
                          agent=agent if agent is not None else cfg.get('agent'),
                          model=model if model is not None else cfg.get('model'))


def ingest_alerts(store, cfg, alerts, now=None, fresh=True, home=None):
    """Consume freshly evaluated dash alerts (evaluation-point contract)."""
    now = time.time() if now is None else now
    out = []
    for a in alerts or []:
        out.append(check_and_record(
            store, cfg, source='alert', kind=a.get('rule', ''),
            key=a.get('key') or '%s|%s' % (a.get('org_repo', ''), a.get('rule', '')),
            payload=dict(a), org_repo=a.get('org_repo', ''),
            severity=a.get('severity', 'mid'), now=now, fresh=fresh, home=home))
    return out


def ingest_event(store, cfg, kind, payload, project='', now=None, fresh=True,
                 home=None):
    """Consume one CP event (the Store.event sink contract: fast, never throws
    into the writer — callers wrap this)."""
    payload = payload or {}
    key = '%s|%s|%s' % (kind, project, payload.get('entity') or '')
    return check_and_record(store, cfg, source='cp_event', kind=kind, key=key,
                            payload=dict(payload, project=project), org_repo=project,
                            severity='high', now=now, fresh=fresh, home=home)


def ingest_watchdog(store, cfg, log_file, db=None, now=None, home=None):
    """Ingest new watchdog.jsonl revival lines behind a line cursor.

    The cursor lives in the dash store meta when a dash db is supplied (the
    collect CLI path), else in the operator store itself. Revival lines become
    watchdog signals; ``watchdog_auto_spawn`` only governs whether PR2's
    dispatcher may round them, the ledger records them either way."""
    now = time.time() if now is None else now
    path = Path(log_file)
    cursor_key = 'operator_watchdog_line'
    holder = db if db is not None else store.db
    try:
        start = int(_meta_get_db(holder, cursor_key, '0'))
    except (ValueError, TypeError):
        start = 0
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    out = []
    for idx, line in enumerate(lines[start:], start=start):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        # Any parseable row advances the cursor — a non-whitelisted action
        # (probe-ok, etc.) is still consumed, so long-running watchdog.jsonl
        # tails are O(new lines), not re-read from a stale cursor each pass.
        if entry.get('action') in WATCHDOG_ACTIONS:
            task = entry.get('task') or 'unknown'
            out.append(check_and_record(
                store, cfg, source='watchdog', kind='watchdog_spawn',
                key='watchdog|%s' % task, payload=dict(entry),
                org_repo=entry.get('org_repo', ''), severity='mid', now=now,
                home=home))
        _meta_set_db(holder, cursor_key, idx + 1)
    return out


def _meta_get_db(db, key, default=None):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row['value'] if row is not None else default


def _meta_set_db(db, key, value):
    db.execute('INSERT INTO meta (key,value) VALUES (?,?) '
               'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))
