"""Evidence read-side: baseline candidates, latest verification signals, and
env-failure accounting. Pure functions over an explicit store/cfg so the
service layer keeps only orchestration.
"""
from __future__ import annotations

import json
import time


def baseline_candidates(store, cfg):
    """Baseline-failure suspects from our own verification evidence.

    A master defect shows up as the same failed node across PR verifies while
    no derivation edge owns it — every loop then stalls on Blocked verdicts it
    can never clear. Candidates ride the discover payload so the discover
    round reproduces them on the base and, on confirmation, opens the fix
    issue through the normal pipeline. Pure read: sync never runs tests.
    Precision filter is the discover reproduction, so PR-caused and flaky
    nodes cost one rejected candidate, not a wrong issue.
    """
    since = time.time() - cfg.get('baseline_window_seconds', 7 * 86400)
    rows = store.db.execute(
        '''SELECT e.payload AS payload, e.created_at AS created_at, w.key AS item_key
           FROM evidence e
           LEFT JOIN executions x ON x.id=e.execution_id
           LEFT JOIN work_items w ON w.id=x.item_id
           WHERE e.kind='verification' AND e.created_at>=?
           ORDER BY e.created_at DESC''', (since,)).fetchall()
    merged = {}
    for row in rows:
        payload = json.loads(row['payload'])
        if payload.get('passed'):
            continue
        head = payload.get('head')
        for check in payload.get('checks', []):
            if check.get('failure_class') not in ('code', 'timeout'):
                continue
            sample = check.get('failed_line')
            for node in check.get('failed_nodes') or []:
                entry = merged.setdefault(node, {'node': node, 'heads': set(), 'sources': set(),
                                                 'last_seen': 0.0, 'sample_line': None})
                if head:
                    entry['heads'].add(head)
                if row['item_key']:
                    entry['sources'].add(row['item_key'])
                entry['last_seen'] = max(entry['last_seen'], row['created_at'] or 0.0)
                if entry['sample_line'] is None and sample:
                    entry['sample_line'] = sample
    ranked = sorted(merged.values(),
                    key=lambda c: (-len(c['heads']), c['last_seen'], c['node']))
    return [{'node': c['node'], 'heads': len(c['heads']), 'sources': sorted(c['sources']),
             'last_seen': c['last_seen'], 'sample_line': c['sample_line']}
            for c in ranked[:int(cfg.get('baseline_max_candidates', 5))]]


def verification_signals(store, cfg):
    """Latest verification fact per review item key, keyed to the head it was
    measured on: attribution verdict, env signature. Feeds the derivation
    edges that turn a survived-red into sync-base repair, base-defect
    escalation, or a frozen-until-gate-fixed review skip.

    Only verdict-delivered rounds (SUCCEEDED) speak here: a signal is a fact
    a round earned by finishing, never a rumor from a round still running —
    an in-flight red would otherwise freeze its own item mid-round — and
    never a haunting from a cancelled one, whose evidence can never be
    refreshed (the freeze would stop anyone from producing a new signal)."""
    since = time.time() - cfg.get('baseline_window_seconds', 7 * 86400)
    rows = store.db.execute(
        '''SELECT e.payload AS payload, e.created_at AS created_at, w.key AS item_key
           FROM evidence e
           JOIN executions x ON x.id=e.execution_id AND x.state='SUCCEEDED'
           JOIN work_items w ON w.id=x.item_id
           WHERE e.kind='verification' AND e.created_at>=?
           ORDER BY e.created_at DESC''', (since,)).fetchall()
    latest = {}
    for row in rows:
        key = row['item_key']
        if not key or not key.startswith('review:') or key in latest:
            continue
        payload = json.loads(row['payload'])
        attribution = payload.get('attribution') or {}
        latest[key] = {'head': payload.get('head'),
                       'env_signature': payload.get('env_signature'),
                       'attribution': attribution.get('verdict'),
                       'base_tip_sha': attribution.get('base_tip_sha')}
    return latest


def prior_env_signatures(store, item_id, execution_id):
    """Signatures this item failed on before the current execution."""
    rows = store.db.execute(
        '''SELECT e.payload AS payload FROM evidence e
           JOIN executions x ON x.id=e.execution_id
           WHERE x.item_id=? AND e.kind='verification' AND e.execution_id!=?''',
        (item_id, execution_id)).fetchall()
    signatures = {json.loads(r['payload']).get('env_signature') for r in rows}
    signatures.discard(None)
    return signatures


def count_env_failure(store, result, execution_id, meta_get, meta_set):
    """Bump the same-signature failure counter; returns the prior count."""
    signature = result.get('env_signature')
    if not signature:
        return 0
    counts = meta_get('env_failures') or {}
    entry = counts.get(signature) or {'count': 0, 'first_seen': time.time()}
    prior = entry['count']
    entry.update(count=entry['count'] + 1, last_seen=time.time(), last_execution=execution_id)
    counts[signature] = entry
    meta_set('env_failures', counts)
    return prior
