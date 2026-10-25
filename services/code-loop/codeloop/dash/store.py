"""Dashboard store facade: the stable ``store.X`` public surface.

The former 1289-line monolith was split along its native sections
(2026-09-16 package restructure): schema (vocabulary/DDL/connection),
ingest (declarative fact writes), verdicts (PR verdict derivation),
views (queues/board/trends), health (alerts/rollups/timeline). Callers
keep the historical ``store.attribute`` access via these explicit
re-exports. Monkeypatches must target the defining module (e.g.
``verdicts._cp_verdicts``), not this facade.
"""
from .health import (LOOP_KINDS, alerts, events, loop_health, overview,
                     worker_summary)
from .ingest import (RUNTIME_TABLES, apply_snapshot, known_items,
                     list_machines, log_sync, record_user_reply,
                     replace_machines, replace_runtime, set_project_trigger,
                     upsert_project)
from .schema import (AGENT_MARKER, ALERT_DEFAULTS, DECISION_ANCHOR,
                     LEGACY_AGENT_MARKER, LINK_RE, NEEDS_HUMAN, SCHEMA,
                     VERDICTS, connect, connect_readonly, get_meta, is_agent_comment, parse_ts,
                     set_meta)
from .verdicts import latest_verdict, latest_verdict_detail, verdicts_for
from .views import (KANBAN_COLUMNS, MERGED_CAP, exceptions, funnel, issue_detail,
                    kanban, pending_consumption, pending_decisions,
                    pending_merges, trends)

__all__ = [
    'AGENT_MARKER', 'ALERT_DEFAULTS', 'DECISION_ANCHOR', 'KANBAN_COLUMNS',
    'LEGACY_AGENT_MARKER', 'LINK_RE', 'LOOP_KINDS', 'MERGED_CAP',
    'NEEDS_HUMAN', 'RUNTIME_TABLES', 'SCHEMA', 'VERDICTS', 'alerts',
    'apply_snapshot', 'connect', 'connect_readonly', 'events', 'exceptions', 'funnel', 'get_meta',
    'is_agent_comment', 'issue_detail', 'kanban', 'known_items',
    'latest_verdict', 'latest_verdict_detail', 'list_machines', 'log_sync',
    'loop_health', 'overview', 'parse_ts', 'pending_consumption',
    'pending_decisions', 'pending_merges', 'record_user_reply',
    'replace_machines', 'replace_runtime', 'set_meta',
    'set_project_trigger', 'trends',
    'upsert_project', 'verdicts_for', 'worker_summary',
]
