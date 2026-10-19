"""Push new dashboard alerts to an IM webhook (wecom / dingtalk / raw).

Dedup is signature-based (rule + project + entity key) and persisted in the
store meta, so an aging timer like needs_human_aging does not re-ping on every
collect. Seen signatures are pruned to the current set, so a resolved alert
that reappears pushes again. Notification failures are reported, never raised:
push must not break the collector.
"""
import json
import sqlite3
import time
import urllib.parse
import urllib.request

from . import store

STYLES = ('wecom', 'dingtalk', 'raw')

# Which dashboard view answers each alert rule; unmapped rules land on the
# kanban board (the merged home view), the right landing page for "go look" pings.
VIEW_BY_RULE = {'loop_stall': 'loops', 'lease_expired': 'loops',
                'outbox_limbo': 'loops', 'collect_stale': 'projects',
                'machine_disabled': 'projects', 'worker_stall': 'loops'}


def signature(alert):
    return '%s|%s|%s' % (alert['rule'], alert['org_repo'], alert.get('key') or '')


def deep_link(base_url, alert):
    view = VIEW_BY_RULE.get(alert.get('rule'), 'kanban')
    project = urllib.parse.quote(alert.get('org_repo') or '')
    return '%s/#p=%s&view=%s' % (base_url.rstrip('/'), project, view)


def render(style, alerts, dashboard_url=None):
    lines = ['code-loop 告警（%d 条新）' % len(alerts)]
    lines += ['- [%s] %s %s' % (a['severity'], a['org_repo'], a['message'])
              for a in alerts]
    if dashboard_url:
        lines += ['面板：%s' % deep_link(dashboard_url, a) for a in alerts]
    text = '\n'.join(lines)
    if style in ('wecom', 'dingtalk'):
        return {'msgtype': 'text', 'text': {'content': text}}
    return {'text': text, 'alerts': alerts}


def _consume_by_operator(on_alerts, fresh, result):
    """Operator subscription (2026-09-30): hand the same fresh list to the
    ingest gate at the evaluation point. Isolation is contractual — the gate
    must never break alerting (the webhook push stays upstream of this)."""
    if on_alerts is None or not fresh:
        return
    try:
        on_alerts(fresh)
    except Exception as exc:
        result['error'] = 'operator ingest failed: %s' % exc


def push_new_alerts(db, config, now=None, push=None, on_alerts=None):
    """Diff current alerts against already-pushed signatures and push new ones.

    on_alerts (optional) receives the same fresh list at the evaluation point
    (operator subscription); its failures are recorded, never raised."""
    now = time.time() if now is None else now
    notify_cfg = (config or {}).get('notify') or {}
    webhook = notify_cfg.get('webhook')
    style = notify_cfg.get('style', 'raw')
    if style not in STYLES:
        style = 'raw'
    alerts = store.alerts(db, now=now, config=(config or {}).get('alerts'))
    current = {signature(a) for a in alerts}
    seen = set(json.loads(store.get_meta(db, 'notified_alerts', '[]')))
    fresh = [a for a in alerts if signature(a) not in seen]
    result = {'new_alerts': len(fresh), 'pushed': 0, 'error': None}
    try:
        store.set_meta(db, 'notified_alerts', json.dumps(sorted(current)))
    except sqlite3.Error as exc:
        # Never crash the collector here (2026-09-14: this exact write killed
        # whole collect runs after a successful collect). Returning before the
        # push keeps exactly-once: nothing goes out this round, the alerts
        # stay fresh and are pushed on the next one.
        result['error'] = 'alert dedup meta not updated: %s' % exc
        return result
    _consume_by_operator(on_alerts, fresh, result)
    if not fresh or not webhook:
        return result
    try:
        (push or _post)(webhook, render(style, fresh,
                                        notify_cfg.get('dashboard_url')))
        result['pushed'] = len(fresh)
    except OSError as exc:
        result['error'] = 'webhook push failed: %s' % exc
    return result


def _post(webhook, payload):
    request = urllib.request.Request(
        webhook, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()
