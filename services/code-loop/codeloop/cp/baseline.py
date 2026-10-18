"""Baseline-failure suspects derived from our own verification evidence."""
from __future__ import annotations

import json
import time


def candidates(db, cfg):
    """Baseline-failure suspects from our own verification evidence.

    A master defect shows up as the same failed node across PR verifies
    while no derivation edge owns it — every loop then stalls on Blocked
    verdicts it can never clear. Candidates ride the discover payload so
    the discover round reproduces them on the base and, on confirmation,
    opens the fix issue through the normal pipeline. Pure read: sync never
    runs tests. Precision filter is the discover reproduction, so
    PR-caused and flaky nodes cost one rejected candidate, not a wrong
    issue.
    """
    since = time.time() - cfg.get('baseline_window_seconds', 7 * 86400)
    rows = db.execute(
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
