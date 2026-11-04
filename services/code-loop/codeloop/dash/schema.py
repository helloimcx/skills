"""Dashboard store schema layer: identity vocabulary, DDL, connection, time helpers.

Split from the former monolithic store.py (2026-09-16 package restructure):
constants and connection primitives shared by every other store layer.
"""
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


# The four reviewer verdict strings from code-loop reviewer protocol.
VERDICTS = ('Approved for manual merge', 'Changes requested', 'Blocked',
            'Validation failed')
NEEDS_HUMAN = 'needs-human'
# Machine marker appended to every agent-published body (Control Plane adds it
# deterministically; loop rules mandate it for direct CLI writes). Its absence
# is the only reliable way to tell a human comment from an agent comment when
# both are posted under the same platform account. The legacy opcode marker
# (<!-- code-loop:<op> -->) also identifies pre-migration agent comments.
AGENT_MARKER = '<!-- code-loop-agent:'
LEGACY_AGENT_MARKER = '<!-- code-loop:'
# The decision-list anchor is always written by the handler, never by a user;
# it identifies pre-migration agent comments that carry no machine marker.
DECISION_ANCHOR = '**决策清单'


def is_agent_comment(body):
    """True when the comment body carries the code-loop agent identity marker
    (new format) or a legacy opcode marker (pre-migration agent comments)."""
    text = body or ''
    return AGENT_MARKER in text or LEGACY_AGENT_MARKER in text or DECISION_ANCHOR in text

LINK_RE = re.compile(r'\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)',
                     re.IGNORECASE)

ALERT_DEFAULTS = {
    'collect_interval': 1800,       # expected collect cadence, seconds
    'collect_stale_factor': 2.5,    # alert when last ok collect older than x interval
    'loop_stall_seconds': 10800 * 2.5,  # default loop period 3h, same factor
    'needs_human_aging_seconds': 86400,
    # Per-kind worker stall thresholds (seconds): expected schedule period × a
    # tolerance factor. discover runs twice a day on this deployment (18h =
    # 1.5 periods, catching one missed round), the other loops every 3h
    # (7.5h = 2.5 periods); override per deployment via dashboard config.
    'worker_stall_seconds': {'discover': 64800, 'handler': 27000, 'reviewer': 27000},
}

# The finite persistent serving domain is declared with the schema so every
# SQLite writer, including mixed-version writers, invalidates it by trigger.
SERVING_SOURCE_TABLES = (
    'projects', 'issues', 'prs', 'comments', 'work_items', 'executions',
    'leases', 'outbox', 'cp_events', 'operator_signals', 'operator_runs',
)

# agent_runs retention: finished subagent archives older than this are swept
# (agentruns.sweep); live rows are never aged out (agentruns re-checks them).
AGENT_RUNS_RETENTION_DAYS = 90


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  org_repo TEXT NOT NULL UNIQUE,
  config_path TEXT NOT NULL DEFAULT '',
  platform_url TEXT NOT NULL DEFAULT '',
  repo TEXT NOT NULL DEFAULT '',
  base TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 0,
  enabled_host TEXT NOT NULL DEFAULT '',
  enabled_at REAL,
  last_collect_at REAL,
  last_ok_collect_at REAL,
  last_error TEXT,
  repo_cwd TEXT,
  entry_doc TEXT,
  rules_doc TEXT,
  spawn_command TEXT,
  coordinator_agent TEXT,
  coordinator_model TEXT
);
CREATE TABLE IF NOT EXISTS issues (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  number INTEGER NOT NULL,
  state TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  body TEXT,
  labels TEXT NOT NULL DEFAULT '[]',
  created_at TEXT,
  updated_at TEXT,
  closed_at TEXT,
  html_url TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL DEFAULT '',
  UNIQUE(project_id, number));
CREATE TABLE IF NOT EXISTS prs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  number INTEGER NOT NULL,
  state TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  body TEXT,
  head_sha TEXT NOT NULL DEFAULT '',
  head_ref TEXT NOT NULL DEFAULT '',
  base_ref TEXT NOT NULL DEFAULT '',
  merged INTEGER NOT NULL DEFAULT 0,
  merged_at TEXT,
  mergeable INTEGER,
  issue_numbers TEXT NOT NULL DEFAULT '[]',
  created_at TEXT,
  updated_at TEXT,
  closed_at TEXT,
  html_url TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL DEFAULT '',
  UNIQUE(project_id, number));
CREATE TABLE IF NOT EXISTS pr_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  pr_number INTEGER NOT NULL,
  reviewer TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  submitted_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  number INTEGER NOT NULL,
  poster TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS user_replies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  number INTEGER NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS work_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  cp_id INTEGER NOT NULL,
  key TEXT,
  kind TEXT,
  state TEXT,
  priority INTEGER,
  version INTEGER,
  payload TEXT,
  resources TEXT,
  created_at TEXT,
  content_hash TEXT,
  UNIQUE(project_id, cp_id));
CREATE TABLE IF NOT EXISTS executions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  cp_id INTEGER NOT NULL,
  item_cp_id INTEGER,
  worker TEXT,
  state TEXT,
  result TEXT,
  worktree TEXT,
  branch TEXT,
  head TEXT,
  expires_at TEXT,
  created_at TEXT,
  content_hash TEXT,
  UNIQUE(project_id, cp_id));
CREATE TABLE IF NOT EXISTS leases (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  resource TEXT,
  execution_cp_id INTEGER,
  token TEXT,
  expires_at TEXT,
  content_hash TEXT);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  cp_id INTEGER NOT NULL,
  execution_cp_id INTEGER,
  action TEXT,
  state TEXT,
  receipt TEXT,
  created_at TEXT,
  content_hash TEXT,
  UNIQUE(project_id, cp_id));
CREATE TABLE IF NOT EXISTS cp_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  cp_id INTEGER NOT NULL,
  kind TEXT,
  payload TEXT,
  created_at TEXT,
  content_hash TEXT,
  UNIQUE(project_id, cp_id));
-- Mirror of the CP command ledger: the executing agent of a loop execution is
-- a ledger fact (the operator click names it), and the execution-history list
-- needs it per row without reopening every project's control-plane store.
CREATE TABLE IF NOT EXISTS commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  cp_id INTEGER NOT NULL,
  action TEXT, number INTEGER, state TEXT,
  execution TEXT, agent TEXT, model TEXT, created_at REAL,
  content_hash TEXT,
  UNIQUE(project_id, cp_id));
-- Archive of proactively-spawned subagent executions derived deterministically
-- from the local CLI session stores (agentruns.py; spec
-- docs/specs/2026-09-21-dash-execution-history.md). One row per subagent run;
-- state uses the loop execution vocabulary (RUNNING/SUCCEEDED/FAILED) so the
-- list and the run view share one chip mapping.
CREATE TABLE IF NOT EXISTS agent_runs (
  id TEXT PRIMARY KEY,
  platform TEXT NOT NULL,
  project TEXT NOT NULL DEFAULT '',
  cwd TEXT,
  task_prompt TEXT,
  parent_ref TEXT,
  state TEXT,
  started_at REAL, ended_at REAL, duration_s INTEGER,
  steps TEXT, tool_counts TEXT, result_head TEXT,
  transcript_ref TEXT, first_seen_at REAL);
CREATE INDEX IF NOT EXISTS idx_agent_runs_time ON agent_runs(started_at DESC);
CREATE TABLE IF NOT EXISTS machines (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 1,
  revoked INTEGER NOT NULL DEFAULT 0,
  created_at REAL,
  last_seen REAL,
  last_addr TEXT,
  agent_version TEXT DEFAULT '',
  projects TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sync_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  started REAL NOT NULL,
  finished REAL NOT NULL,
  ok INTEGER NOT NULL,
  stats TEXT NOT NULL DEFAULT '{}',
  error TEXT
);
-- Read-path indexes (project-switch latency): without them every per-PR
-- verdict lookup full-scanned comments (2270 such queries ≈ 10s of SQL per
-- overview+items+trends render on the live store) and every per-project
-- board/queue query scanned its whole table. connect() runs this script on
-- existing stores too, so upgrades pick them up on first reopen; at this
-- store's scale the write-side cost is noise.
CREATE INDEX IF NOT EXISTS idx_comments_scope ON comments(project_id, kind, number);
CREATE INDEX IF NOT EXISTS idx_issues_scope ON issues(project_id, state);
CREATE INDEX IF NOT EXISTS idx_prs_scope ON prs(project_id, state);
CREATE INDEX IF NOT EXISTS idx_exec_scope ON executions(project_id);
-- Covering index for loop_health's last-activity scan: GROUP BY + MAX read
-- only these columns, never the payload-bearing table rows.
CREATE INDEX IF NOT EXISTS idx_wi_cover ON work_items(project_id, kind, created_at);
-- Operator mirror (2026-09-30): machine-level projections of the operator
-- store (pure read models — the operator store is the only source of truth).
CREATE TABLE IF NOT EXISTS operator_signals (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    org_repo TEXT NOT NULL DEFAULT '',
    severity TEXT,
    message TEXT,
    payload TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS operator_runs (
    id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    worker TEXT NOT NULL,
    state TEXT NOT NULL,
    result TEXT,
    agent TEXT,
    model TEXT,
    started_at REAL NOT NULL,
    ended_at REAL
);
CREATE TABLE IF NOT EXISTS serving_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS serving_cache (
  key TEXT PRIMARY KEY,
  body BLOB NOT NULL,
  gzip_body BLOB NOT NULL,
  source_revision INTEGER NOT NULL,
  format_version INTEGER NOT NULL
);
"""


def connect(path):
    # check_same_thread=False permits sequential handoff to a consumer thread;
    # callers must prevent overlapping use. SQLite builds with THREADSAFE=2
    # do not serialize a shared connection (as on Apple's system Python).
    # Production HTTP reads use separate connect_readonly() connections.
    # timeout=15: the busy-handler window for writer-writer contention — a
    # locked store must slow a collector down, never crash it (2026-09-14
    # starvation: 1222 "database is locked" crashes silently starved the
    # alphabetically later projects).
    # WAL: serve readers and the collector no longer block each other's
    # commits. The switch is persistent; if the store is busy right now it
    # stays in its old mode and the next connect retries.
    # isolation_level=None (autocommit): a write transaction can never
    # outlive its statement. Under the implicit-transaction default, one
    # uncommitted meta upsert on the shared consumer connection held the
    # store's write lock for hours and froze every writer (2026-10-15,
    # issue #366). Every store write is a single idempotent upsert; nothing
    # depends on implicit multi-statement atomicity.
    db = sqlite3.connect(str(path), timeout=15.0, check_same_thread=False,
                         isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        db.execute('PRAGMA journal_mode=WAL')
    except sqlite3.OperationalError:
        pass  # a concurrent writer holds the switch; retry on next connect
    db.executescript(SCHEMA)
    db.execute("INSERT OR IGNORE INTO serving_meta(key,value) "
               "VALUES ('source_revision','0')")
    # Project collection timestamps/config updates do not affect any fixed
    # builder; only project identity or removal changes their response shape.
    db.execute('DROP TRIGGER IF EXISTS serving_revision_projects_update')
    for table in SERVING_SOURCE_TABLES:
        operations = (('INSERT', 'insert'), ('DELETE', 'delete'))
        if table == 'projects':
            operations += (('UPDATE OF id,org_repo', 'update_identity'),)
        else:
            operations += (('UPDATE', 'update'),)
        for operation, suffix in operations:
            trigger = 'serving_revision_%s_%s' % (table, suffix)
            db.execute('''CREATE TRIGGER IF NOT EXISTS "%s" AFTER %s ON "%s"
                BEGIN UPDATE serving_meta SET value=CAST(value AS INTEGER)+1
                WHERE key='source_revision'; END''' % (trigger, operation, table))
    # Runtime mirrors were historically replaced wholesale. Add the per-row
    # projection fingerprint in place so old stores can switch to idempotent
    # synchronization without rebuilding tables or changing their row ids.
    for table in ('work_items', 'executions', 'leases', 'outbox', 'cp_events',
                  'commands'):
        columns = {row[1] for row in db.execute('PRAGMA table_info(%s)' % table)}
        if 'content_hash' not in columns:
            try:
                db.execute('ALTER TABLE %s ADD COLUMN content_hash TEXT' % table)
            except sqlite3.OperationalError:
                pass  # a concurrent connect() added it first
    # Lightweight migration for stores created before the mergeable column.
    columns = {row[1] for row in db.execute('PRAGMA table_info(prs)')}
    if 'mergeable' not in columns:
        try:
            db.execute('ALTER TABLE prs ADD COLUMN mergeable INTEGER')
        except sqlite3.OperationalError:
            pass  # a concurrent connect() added it first
    # …and before the manual review-trigger registration columns.
    columns = {row[1] for row in db.execute('PRAGMA table_info(projects)')}
    for column in ('repo_cwd', 'entry_doc', 'rules_doc', 'spawn_command',
                   'coordinator_agent', 'coordinator_model'):
        if column not in columns:
            try:
                db.execute('ALTER TABLE projects ADD COLUMN %s TEXT' % column)
            except sqlite3.OperationalError:
                pass  # a concurrent connect() added it first
    return db


def connect_readonly(path):
    """Open an existing dashboard for one read operation on its owning thread.

    URI encoding preserves paths containing #, ? or %. No schema or journal
    initialization runs here; the listener initializes the database once.
    """
    db = sqlite3.connect(Path(path).expanduser().resolve().as_uri() + '?mode=ro',
                         uri=True, timeout=15.0, isolation_level=None)
    db.row_factory = sqlite3.Row
    return db


def get_meta(db, key, default=None):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row['value'] if row is not None else default


def set_meta(db, key, value):
    with db:
        db.execute('INSERT INTO meta (key, value) VALUES (?,?) '
                   'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))


def parse_ts(value):
    """Timestamp -> epoch seconds. Control-plane stores epoch (often as text);
    Gitea returns ISO-8601. Returns None when absent or unparseable."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _hash(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode('utf-8')
    ).hexdigest()
