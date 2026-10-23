"""Dashboard store ingest layer: declarative fact writes and runtime replace.

All writes go through apply_snapshot / replace_runtime so collectors stay
declarative: content-hash diff upsert for platform objects, wholesale
replacement for the small mutable control-plane runtime tables.
"""
import hashlib
import json
from decimal import Decimal, InvalidOperation

from .schema import LINK_RE, _hash


def upsert_project(db, org_repo, config_path='', **fields):
    allowed = ('platform_url', 'repo', 'base', 'enabled', 'enabled_host',
               'enabled_at', 'last_collect_at', 'last_ok_collect_at',
               'last_error')
    with db:
        row = db.execute('SELECT id FROM projects WHERE org_repo=?',
                         (org_repo,)).fetchone()
        if row is None:
            cur = db.execute('INSERT INTO projects (org_repo, config_path) VALUES (?,?)',
                             (org_repo, config_path))
            pid = cur.lastrowid
        else:
            pid = row['id']
            if config_path:
                db.execute('UPDATE projects SET config_path=? WHERE id=?',
                           (config_path, pid))
        for key, value in fields.items():
            if key in allowed:
                db.execute('UPDATE projects SET %s=? WHERE id=?' % key, (value, pid))
    return pid


def set_project_trigger(db, org_repo, **fields):
    """Register or edit one project's manual review-trigger facts (edit
    semantics: only provided keys are written; the project row is created on
    demand and a later collect fills config_path without clobbering these
    columns). Keys: repo_cwd / entry_doc / rules_doc (non-empty strings) and
    spawn_command (list of argv strings, stored JSON-encoded). Raises
    ValueError on unknown keys or bad value types."""
    unknown = set(fields) - {'repo_cwd', 'entry_doc', 'rules_doc', 'spawn_command'}
    if unknown:
        raise ValueError('unknown keys ' + ','.join(sorted(unknown)))
    updates = {}
    for key in ('repo_cwd', 'entry_doc', 'rules_doc'):
        value = fields.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            raise ValueError('%s must be a non-empty string' % key)
        updates[key] = value
    if 'spawn_command' in fields and fields['spawn_command'] is not None:
        command = fields['spawn_command']
        if (not isinstance(command, list)
                or not all(isinstance(x, str) and x for x in command)):
            raise ValueError('spawn_command must be a non-empty string list')
        updates['spawn_command'] = json.dumps(command)
    with db:
        row = db.execute('SELECT id FROM projects WHERE org_repo=?',
                         (org_repo,)).fetchone()
        if row is None:
            cur = db.execute('INSERT INTO projects (org_repo) VALUES (?)', (org_repo,))
            pid = cur.lastrowid
        else:
            pid = row['id']
        for key, value in updates.items():
            db.execute('UPDATE projects SET %s=? WHERE id=?' % key, (value, pid))
    return pid


def log_sync(db, pid, ok, stats, started, finished, error=None):
    with db:
        db.execute('INSERT INTO sync_log (project_id, started, finished, ok, stats, error) '
                   'VALUES (?,?,?,?,?,?)',
                   (pid, started, finished, 1 if ok else 0,
                    json.dumps(stats, ensure_ascii=False), error))


def _labels(issue):
    return [x.get('name', '') for x in issue.get('labels') or []
            if isinstance(x, dict)]


def _upsert_fact(db, table, pid, number, row):
    """Content-hash diff upsert. Returns (added|updated|unchanged, row_id).

    The fresh-insert path is a single ``INSERT ... ON CONFLICT DO UPDATE``,
    not a SELECT-then-INSERT: with no process lock, two collectors can ingest
    the same new row concurrently (2026-09-23), and a plain INSERT would let
    the loser die on the UNIQUE(project_id, number) constraint — recording a
    spurious project failure. The upsert makes the race a no-op: one inserts,
    the other updates the same row. ``lastrowid`` is the rowid of the inserted
    row, or of the updated row when the insert was converted to an update."""
    digest = _hash(row)
    old = db.execute('SELECT id, content_hash FROM %s WHERE project_id=? AND number=?' % table,
                     (pid, number)).fetchone()
    if old is not None and old['content_hash'] == digest:
        return 'unchanged', old['id']
    row = dict(row, content_hash=digest)
    columns = ','.join(row)
    marks = ','.join('?' * len(row))
    if old is None:
        assigns = ','.join('%s=excluded.%s' % (k, k) for k in row)
        with db:
            cur = db.execute(
                'INSERT INTO %s (project_id, number, %s) VALUES (?,?,%s) '
                'ON CONFLICT(project_id, number) DO UPDATE SET %s'
                % (table, columns, marks, assigns), (pid, number, *row.values()))
            return 'added', cur.lastrowid
    with db:
        db.execute('UPDATE %s SET %s WHERE id=?'
                   % (table, ','.join(k + '=?' for k in row)), (*row.values(), old['id']))
    return 'updated', old['id']


def _apply_issue(db, pid, issue):
    row = {
        'state': issue['state'], 'title': issue.get('title') or '',
        'body': issue.get('body'), 'labels': json.dumps(_labels(issue), ensure_ascii=False),
        'created_at': issue.get('created_at'), 'updated_at': issue.get('updated_at'),
        'closed_at': issue.get('closed_at'),
        'html_url': issue.get('html_url') or '',
    }
    outcome, row_id = _upsert_fact(db, 'issues', pid, issue['number'], row)
    # Verdict/decision comments land on items whose own fields did not change;
    # open items are few, so always refresh their comments for freshness.
    if outcome != 'unchanged' or issue['state'] == 'open':
        with db:
            db.execute('DELETE FROM comments WHERE project_id=? AND kind=? AND number=?',
                       (pid, 'issue', issue['number']))
            for item in issue.get('comments') or []:
                db.execute('INSERT INTO comments (project_id, kind, number, poster, body, created_at) '
                           'VALUES (?,?,?,?,?,?)',
                           (pid, 'issue', issue['number'],
                            (item.get('user') or {}).get('login', ''),
                            item.get('body') or '', item.get('created_at') or ''))
    return outcome


def _apply_pr(db, pid, pr):
    linked = sorted({int(x) for x in LINK_RE.findall(pr.get('body') or '')})
    row = {
        'state': pr['state'], 'title': pr.get('title') or '',
        'body': pr.get('body'),
        'head_sha': (pr.get('head') or {}).get('sha', ''),
        'head_ref': (pr.get('head') or {}).get('ref', ''),
        'base_ref': (pr.get('base') or {}).get('ref', ''),
        'merged': 1 if pr.get('merged') else 0,
        'merged_at': pr.get('merged_at'),
        'mergeable': None if pr.get('mergeable') is None else (1 if pr.get('mergeable') else 0),
        'issue_numbers': json.dumps(linked),
        'created_at': pr.get('created_at'), 'updated_at': pr.get('updated_at'),
        'closed_at': pr.get('closed_at'),
        'html_url': pr.get('html_url') or '',
    }
    outcome, row_id = _upsert_fact(db, 'prs', pid, pr['number'], row)
    if outcome != 'unchanged' or pr['state'] == 'open':
        with db:
            db.execute('DELETE FROM comments WHERE project_id=? AND kind=? AND number=?',
                       (pid, 'pr', pr['number']))
            db.execute('DELETE FROM pr_reviews WHERE project_id=? AND pr_number=?',
                       (pid, pr['number']))
            for item in pr.get('comments') or []:
                db.execute('INSERT INTO comments (project_id, kind, number, poster, body, created_at) '
                           'VALUES (?,?,?,?,?,?)',
                           (pid, 'pr', pr['number'],
                            (item.get('user') or {}).get('login', ''),
                            item.get('body') or '', item.get('created_at') or ''))
            for item in pr.get('reviews') or []:
                db.execute('INSERT INTO pr_reviews (project_id, pr_number, reviewer, state, body, submitted_at) '
                           'VALUES (?,?,?,?,?,?)',
                           (pid, pr['number'], (item.get('user') or {}).get('login', ''),
                            item.get('state') or '', item.get('body') or '',
                            item.get('submitted_at') or ''))
    return outcome


def apply_snapshot(db, pid, snapshot):
    """Diff-apply a Gitea snapshot; returns per-entity change counts."""
    counts = {'issues_added': 0, 'issues_updated': 0, 'issues_unchanged': 0,
              'prs_added': 0, 'prs_updated': 0, 'prs_unchanged': 0}
    for issue in snapshot.get('issues') or []:
        counts['issues_' + _apply_issue(db, pid, issue)] += 1
    for pr in snapshot.get('prs') or []:
        counts['prs_' + _apply_pr(db, pid, pr)] += 1
    return counts


def known_items(db, pid):
    """{(kind, number): (state, updated_at)} — this store's incremental basis.

    Passed to ``observer.snapshot`` as the crawl's ``known`` (issue #165): a
    closed+unchanged PR this store has never seen (dashboard.sqlite reset or
    first deploy) is fully crawled instead of skipped by the observer's own
    basis and landing with empty comments/reviews.
    """
    rows = db.execute(
        "SELECT 'issue' AS kind, number, state, updated_at FROM issues WHERE project_id=?"
        " UNION ALL SELECT 'pr', number, state, updated_at FROM prs WHERE project_id=?",
        (pid, pid)).fetchall()
    return {(r['kind'], r['number']): (r['state'], r['updated_at']) for r in rows}


RUNTIME_TABLES = {
    'work_items': [('cp_id', 'id'), ('key', 'key'), ('kind', 'kind'),
                   ('state', 'state'), ('priority', 'priority'), ('version', 'version'),
                   ('payload', 'payload'), ('resources', 'resources'),
                   ('created_at', 'created_at')],
    'executions': [('cp_id', 'id'), ('item_cp_id', 'item_id'), ('worker', 'worker'),
                   ('state', 'state'), ('result', 'result'), ('worktree', 'worktree'),
                   ('branch', 'branch'), ('head', 'head'), ('expires_at', 'expires_at'),
                   ('created_at', 'created_at')],
    'leases': [('resource', 'resource'), ('execution_cp_id', 'execution_id'),
               ('token', 'token'), ('expires_at', 'expires_at')],
    'outbox': [('cp_id', 'id'), ('execution_cp_id', 'execution_id'),
               ('action', 'action'), ('state', 'state'), ('receipt', 'receipt'),
               ('created_at', None)],
    'cp_events': [('cp_id', 'id'), ('kind', 'kind'), ('payload', 'payload'),
                  ('created_at', 'created_at')],
    # Command ledger mirror: the execution-history list resolves a loop
    # execution's agent from the ledger without reopening the project store.
    'commands': [('cp_id', 'id'), ('action', 'action'), ('number', 'number'),
                 ('state', 'state'), ('execution', 'execution'), ('agent', 'agent'),
                 ('model', 'model'), ('created_at', 'created_at')],
}


def replace_runtime(db, pid, runtime):
    """Diff-apply one project's six CP mirrors as one atomic projection.

    The content hash covers the exact SQLite-bound representation (including
    JSON strings), while the natural identity stays separate. On a legacy row
    with a NULL hash we compare projected columns using SQLite's own affinity
    rules and backfill the hash without rewriting unchanged facts.
    """
    identities = {table: ('resource' if table == 'leases' else 'cp_id')
                  for table in RUNTIME_TABLES}
    nested = db.in_transaction
    if nested:
        db.execute('SAVEPOINT replace_runtime')
    else:
        db.execute('BEGIN IMMEDIATE')
    try:
        for table, columns in RUNTIME_TABLES.items():
            identity = identities[table]
            numeric_identity = identity != 'resource'
            names = [name for name, _ in columns]
            projected = []
            seen = set()
            for source in runtime.get(table) or []:
                values = []
                for name, source_name in columns:
                    value = source.get(source_name)
                    if isinstance(value, (dict, list)):
                        value = json.dumps(value, ensure_ascii=False)
                    values.append(value)
                value_by_name = dict(zip(names, values))
                identity_key = _runtime_identity_key(
                    value_by_name[identity], numeric=numeric_identity,
                    require_sqlite_integer=numeric_identity)
                if identity_key in seen:
                    raise ValueError('duplicate %s runtime identity %r'
                                     % (table, value_by_name[identity]))
                seen.add(identity_key)
                digest = hashlib.sha256(json.dumps(
                    values, ensure_ascii=False, separators=(',', ':'),
                    default=str).encode('utf-8')).hexdigest()
                projected.append((identity_key, value_by_name[identity],
                                  values, digest))

            if not projected:
                present = db.execute('SELECT 1 FROM %s WHERE project_id=? LIMIT 1'
                                     % table, (pid,)).fetchone()
                if present is not None:
                    db.execute('DELETE FROM %s WHERE project_id=?' % table, (pid,))
                continue

            current_rows = db.execute(
                'SELECT id, %s, content_hash FROM %s WHERE project_id=? ORDER BY id'
                % (identity, table), (pid,)).fetchall()
            by_identity = {}
            for row in current_rows:
                key = _runtime_identity_key(row[identity], numeric=numeric_identity)
                by_identity.setdefault(key, []).append(row)

            names_sql = ','.join(names)
            marks = ','.join('?' for _ in range(len(names) + 2))
            update_sql = 'UPDATE %s SET %s WHERE id=?' % (
                table, ','.join('%s=?' % name for name in names + ['content_hash']))
            insert_sql = 'INSERT INTO %s (project_id,%s,content_hash) VALUES (%s)' % (
                table, names_sql, marks)
            retained = set()
            removed = set()
            for key, identity_value, values, digest in projected:
                matches = by_identity.get(key, [])
                current = matches[0] if matches else None
                if current is None:
                    db.execute(insert_sql, (pid, *values, digest))
                    continue
                retained.add(current['id'])
                if current['content_hash'] == digest:
                    pass
                elif current['content_hash'] is None:
                    unchanged = db.execute(
                        'SELECT 1 FROM %s WHERE id=? AND project_id=? AND %s'
                        % (table, ' AND '.join('%s IS ?' % name for name in names)),
                        (current['id'], pid, *values)).fetchone() is not None
                    if unchanged:
                        db.execute('UPDATE %s SET content_hash=? WHERE id=?'
                                   % table, (digest, current['id']))
                    else:
                        db.execute(update_sql, (*values, digest, current['id']))
                else:
                    db.execute(update_sql, (*values, digest, current['id']))
                # Historic wholesale replacement never left duplicate lease
                # rows, but tolerate them and converge to the source PK.
                for duplicate in matches[1:]:
                    db.execute('DELETE FROM %s WHERE id=?' % table,
                               (duplicate['id'],))
                    removed.add(duplicate['id'])

            for row in current_rows:
                if row['id'] not in retained and row['id'] not in removed:
                    # Rows represented by a source identity were retained
                    # above; all others disappeared upstream.
                    db.execute('DELETE FROM %s WHERE id=?' % table, (row['id'],))
        if nested:
            db.execute('RELEASE SAVEPOINT replace_runtime')
        else:
            db.commit()
    except BaseException:
        if nested:
            db.execute('ROLLBACK TO SAVEPOINT replace_runtime')
            db.execute('RELEASE SAVEPOINT replace_runtime')
        else:
            db.rollback()
        raise


def _runtime_identity_key(value, numeric=True, require_sqlite_integer=False):
    """Make numeric-affinity identities exact across int/text database reads.

    CP ids are projected into INTEGER-affinity columns. Numeric ids outside
    SQLite's signed 64-bit integer range, or fractional numeric ids, cannot be
    represented there without coercion to REAL and possible precision loss.
    Reject those source values before applying any table changes.
    """
    if value is None:
        return ('null', '')
    if not numeric:
        # leases.resource is declared TEXT by the CP store. Leading zeroes,
        # case and punctuation are part of this natural key.
        return ('text', str(value))
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, (int, float, str)):
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError, OverflowError):
            number = None
        if number is not None and number.is_finite():
            sign, digits, exponent = number.as_tuple()
            digits = list(digits)
            if require_sqlite_integer:
                integer = (not any(digits) or exponent >= 0 or (
                    -exponent <= len(digits)
                    and all(digit == 0 for digit in digits[exponent:])))
                if not integer:
                    raise ValueError('numeric CP id must be an integer: %r' % value)
                if (number < Decimal('-9223372036854775808')
                        or number > Decimal('9223372036854775807')):
                    raise ValueError(
                        'numeric CP id is outside SQLite INTEGER range: %r' % value)
            if not any(digits):
                return ('number', 0, (0,), 0)
            while len(digits) > 1 and digits[-1] == 0:
                digits.pop()
                exponent += 1
            return ('number', sign, tuple(digits), exponent)
    return ('text', str(value))


def record_user_reply(db, pid, number, body, created_at):
    """Record a decision reply posted through the dashboard. `created_at` is
    the platform timestamp returned by Gitea for the posted comment, so the
    comparison against collected comments is clock-skew free."""
    with db:
        db.execute('INSERT INTO user_replies (project_id, number, body, created_at) '
                   'VALUES (?,?,?,?)', (pid, number, body, created_at))


def replace_machines(db, machines):
    """Server-scoped machine registry snapshot (collect-time wholesale replace)."""
    with db:
        db.execute('DELETE FROM machines')
        for machine in machines:
            db.execute('INSERT OR REPLACE INTO machines'
                       '(id,name,enabled,revoked,created_at,last_seen,last_addr,agent_version,projects) '
                       'VALUES (?,?,?,?,?,?,?,?,?)',
                       (machine.get('id'), machine.get('name'), machine.get('enabled', 0),
                        machine.get('revoked', 0), machine.get('created_at'),
                        machine.get('last_seen'), machine.get('last_addr'),
                        machine.get('agent_version', ''), machine.get('projects', '')))

def list_machines(db):
    return [dict(row) for row in db.execute(
        'SELECT * FROM machines ORDER BY (enabled=1 AND revoked=0) DESC, last_seen DESC, name')]
