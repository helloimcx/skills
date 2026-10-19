"""Operator machine configuration: the `operator` section of the dashboard
config (~/.code-loop/dashboard.json). Absent section = disabled; unknown keys
are rejected (fail-closed, same discipline as the trigger config). The agent
pair is validated against the runner registry so a typo never reaches spawn.
"""
from .. import agent_runners

DEFAULTS = {
    'enabled': False,
    'agent': None,
    'model': None,
    'cooldown_minutes': 30,
    'max_rounds_per_day': 12,
    # Alert rules eligible for operator rounds. needs_human_aging is
    # deliberately absent: human decisions are never agent-consumed.
    'alert_rules': ('loop_stall', 'worker_stall', 'collect_stale',
                    'lease_expired', 'outbox_limbo', 'pr_blocked',
                    'machine_disabled'),
    'event_kinds': ('sync.failed', 'command.abandon_failed'),
    # Watchdog revival records are ledger-only unless this is turned on.
    'watchdog_auto_spawn': False,
}
KNOWN = frozenset(DEFAULTS)


def _validate_scalars(cfg):
    if not isinstance(cfg['enabled'], bool):
        raise ValueError('operator.enabled must be a boolean')
    if not isinstance(cfg['cooldown_minutes'], (int, float)) or cfg['cooldown_minutes'] <= 0:
        raise ValueError('operator.cooldown_minutes must be a positive number')
    if not isinstance(cfg['max_rounds_per_day'], int) or cfg['max_rounds_per_day'] <= 0:
        raise ValueError('operator.max_rounds_per_day must be a positive integer')


def _validate_lists(cfg):
    for name in ('alert_rules', 'event_kinds'):
        if not cfg[name] or not all(isinstance(x, str) and x for x in cfg[name]):
            raise ValueError('operator.%s must be non-empty strings' % name)


def _validate_agent(cfg):
    if cfg['agent'] is not None:
        # Single enqueue-side gate: unknown runner or malformed model dies here.
        cfg['agent'], cfg['model'] = agent_runners.validated(cfg['agent'], cfg['model'])
    elif cfg['model'] is not None:
        raise ValueError('operator.model requires operator.agent')


def load_operator_config(dashboard_config):
    """Validate and merge the `operator` section; None when absent/not a map."""
    section = (dashboard_config or {}).get('operator')
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ValueError('operator config must be a mapping')
    unknown = sorted(set(section) - KNOWN)
    if unknown:
        raise ValueError('unknown operator config keys: %s' % ', '.join(unknown))
    cfg = dict(DEFAULTS)
    cfg.update(section)
    cfg['alert_rules'] = tuple(cfg['alert_rules'])
    cfg['event_kinds'] = tuple(cfg['event_kinds'])
    _validate_scalars(cfg)
    _validate_lists(cfg)
    _validate_agent(cfg)
    return cfg
