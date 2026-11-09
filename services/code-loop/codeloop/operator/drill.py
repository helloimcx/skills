"""Safe, deterministic Operator signal-gate drills using an isolated store."""
import tempfile

from . import config, ingest, store


SCENARIOS = (
    {'name': 'worker-disconnected', 'rule': 'worker_stall', 'key': 'drill-worker',
     'expected': 'fired', 'action': 'diagnose worker and propose service.start_if_missing'},
    {'name': 'collection-stale', 'rule': 'collect_stale', 'key': 'drill-collect',
     'expected': 'fired', 'action': 'propose collect_refresh'},
    {'name': 'model-unavailable', 'rule': 'model_unavailable', 'key': 'drill-model',
     'expected': 'skipped_rule', 'action': 'escalate to human; no model call'},
)


def run_drill():
    """Run built-in scenarios against a throwaway SQLite store; never dispatch."""
    results = []
    with tempfile.TemporaryDirectory(prefix='code-loop-operator-drill-') as home:
        db = store.open_store(home)
        try:
            cfg = config.load_operator_config({'operator': {'enabled': True}})
            for scenario in SCENARIOS:
                alert = {'rule': scenario['rule'], 'key': scenario['key'],
                         'org_repo': 'drill__isolated', 'severity': 'high'}
                decision = ingest.ingest_alerts(db, cfg, [alert], home=home)[0]
                passed = decision['decision'] == scenario['expected']
                results.append({
                    'scenario': scenario['name'], 'input': alert,
                    'signals': [decision],
                    'diagnosis': decision.get('reason') or decision['decision'],
                    'proposed_actions': [scenario['action']],
                    'assertions': {'expected_decision': scenario['expected'],
                                   'passed': passed},
                })
        finally:
            db.db.close()
    return {'mode': 'isolated-dry-run', 'model_calls': 0,
            'production_actions': 0, 'passed': all(
                r['assertions']['passed'] for r in results), 'scenarios': results}
