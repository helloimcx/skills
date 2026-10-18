"""Dashboard store verdict layer: PR verdict derivation.

Newest verdict per PR from control-plane structured results and platform
comments. ``_cp_verdicts`` is the single patch point: every caller
(including views.trends via the module attribute) resolves it here, so
monkeypatches must target ``verdicts._cp_verdicts``.
"""
import json

from .schema import VERDICTS, parse_ts


def _cp_verdicts(db, pid):
    """{pr_number: (decision, epoch, reviewer, summary)} from control-plane
    reviewer/handler executions, whose result.decision is enum-validated at
    report time. Handler BLOCKED verdicts map into the reviewer vocabulary
    ('Blocked') so repair dead-ends surface on the board even before any
    visibility comment is collected; APPROVE/RESOLVED/REQUEST_CHANGES stay
    invisible (they are not PR review verdicts)."""
    best = {}
    rows = db.execute(
        "SELECT wi.payload, ex.result, ex.created_at FROM work_items wi "
        "JOIN executions ex ON ex.project_id=wi.project_id AND ex.item_cp_id=wi.cp_id "
        "WHERE wi.project_id=? AND wi.kind IN ('reviewer','handler')", (pid,))
    for row in rows:
        try:
            pr = (json.loads(row['payload'] or '{}').get('pr') or {})
            pr_number = int(pr.get('number', 0))
            result = json.loads(row['result']) if row['result'] else {}
        except ValueError:
            continue
        decision = {'BLOCKED': 'Blocked'}.get((result or {}).get('decision'),
                                              (result or {}).get('decision'))
        if pr_number <= 0 or decision not in VERDICTS:
            continue
        moment = parse_ts(row['created_at']) or 0
        known = best.get(pr_number)
        if known is None or moment >= known[1]:
            best[pr_number] = (decision, moment, (result or {}).get('reviewer') or '',
                               (result or {}).get('summary') or '')
    return best


def latest_verdict_detail(db, pid, pr_number, cp_verdicts=None):
    """Newest verdict as {verdict, at, poster, body}: prefer the newer of the
    control-plane structured decision (body = summary) and the latest
    verdict-bearing PR comment (comments wrap the verdict in markdown).
    cp_verdicts: optional precomputed map from _cp_verdicts(db, pid) — callers
    looping over many PRs pass it once instead of triggering a full
    work_items scan per PR (issue #130)."""
    best_cp = (cp_verdicts if cp_verdicts is not None
               else _cp_verdicts(db, pid)).get(pr_number)
    best_c = None
    rows = db.execute("SELECT poster, body, created_at FROM comments "
                      "WHERE project_id=? AND kind='pr' AND number=?",
                      (pid, pr_number)).fetchall()
    for row in rows:
        text = row['body'] or ''
        verdict = next((v for v in VERDICTS if v in text), None)
        if verdict is None:
            continue
        moment = parse_ts(row['created_at']) or 0
        if best_c is None or moment >= best_c[1]:
            best_c = (verdict, moment, row['poster'], text)
    if best_cp is not None and (best_c is None or best_cp[1] >= best_c[1]):
        return {'verdict': best_cp[0], 'at': best_cp[1],
                'poster': best_cp[2], 'body': best_cp[3]}
    if best_c is None:
        return None
    return {'verdict': best_c[0], 'at': best_c[1],
            'poster': best_c[2], 'body': best_c[3]}


def latest_verdict(db, pid, pr_number, cp_verdicts=None):
    """(verdict, epoch, poster) for a PR, or None."""
    detail = latest_verdict_detail(db, pid, pr_number, cp_verdicts=cp_verdicts)
    return (detail['verdict'], detail['at'], detail['poster']) if detail else None


def verdicts_for(db, rows):
    """{project_id: _cp_verdicts map} for the given pr rows, one scan per
    project instead of one per PR (issue #130: trends/_items were paying a
    full work_items scan per PR — 553 scans ≈ 28-50s on the live store)."""
    return {pid: _cp_verdicts(db, pid) for pid in {r['project_id'] for r in rows}}
