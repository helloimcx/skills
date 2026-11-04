"""Pure mirror-backed read models and their disposable persistent encoding."""
import gzip
import json
import time
from urllib.parse import parse_qsl

from . import store


FORMAT_VERSION = 1
DEFAULT_BUILD_SECONDS = 30.0
DEFAULT_BATCH_BYTES = 128 * 1024 * 1024


def items(db):
    issues = [dict(r) for r in db.execute(
        'SELECT i.*, pr.org_repo FROM issues i JOIN projects pr ON pr.id=i.project_id '
        'ORDER BY pr.org_repo, i.number')]
    prs = []
    rows = [dict(r) for r in db.execute(
        'SELECT p.*, pr.org_repo FROM prs p JOIN projects pr '
        'ON pr.id=p.project_id ORDER BY pr.org_repo, p.number')]
    verdicts = store.verdicts_for(db, rows)
    for pr in rows:
        verdict = store.latest_verdict(db, pr['project_id'], pr['number'],
                                       cp_verdicts=verdicts[pr['project_id']])
        pr['verdict'] = verdict[0] if verdict else None
        pr['verdict_at'] = verdict[1] if verdict else None
        prs.append(pr)
    return {'issues': issues, 'prs': prs}


def runtime(db, org_repo):
    projects = {r['org_repo']: r['id'] for r in db.execute('SELECT id, org_repo FROM projects')}
    pid = projects.get(org_repo)
    if pid is None:
        return None
    tables = ('work_items', 'executions', 'leases', 'outbox', 'cp_events')
    columns = {
        'work_items': 'id,project_id,cp_id,key,kind,state,priority,version,payload,resources,created_at',
        'executions': 'id,project_id,cp_id,item_cp_id,worker,state,result,worktree,branch,head,expires_at,created_at',
        'leases': 'id,project_id,resource,execution_cp_id,token,expires_at',
        'outbox': 'id,project_id,cp_id,execution_cp_id,action,state,receipt,created_at',
        'cp_events': 'id,project_id,cp_id,kind,payload,created_at',
    }
    data = {}
    for table in tables:
        data[table] = [dict(row) for row in db.execute(
            'SELECT %s FROM %s WHERE project_id=? ORDER BY id DESC LIMIT 200'
            % (columns[table], table), (pid,))]
    return data


def operator_ledger(db, query=None):
    """Default or requested bounded operator ledger (signal → runs)."""
    query = query or {}
    try:
        limit = max(1, min(500, int(query.get('limit', 50))))
    except (TypeError, ValueError):
        limit = 50
    total = db.execute('SELECT count(*) FROM operator_signals').fetchone()[0]
    signals = db.execute('SELECT * FROM operator_signals '
                         'ORDER BY created_at DESC, id DESC LIMIT ?',
                         (limit,)).fetchall()
    runs = {}
    if signals:
        ids = [signal['id'] for signal in signals]
        marks = ','.join('?' * len(ids))
        for row in db.execute(
                'SELECT * FROM operator_runs WHERE signal_id IN (%s) '
                'ORDER BY started_at' % marks, ids):
            runs.setdefault(row['signal_id'], []).append(dict(row))
    rows = []
    for signal_row in signals:
        signal = dict(signal_row)
        payload = json.loads(signal['payload'])
        out = {'id': signal['id'],
               'signal': {'source': signal['source'], 'kind': signal['kind'],
                          'key': signal['key'], 'org_repo': signal['org_repo'],
                          'severity': payload.get('severity'),
                          'message': payload.get('message'),
                          'note': payload.get('note'),
                          'received_at': signal['created_at']},
               'state': signal['state'], 'rounds': runs.get(signal['id'], [])}
        run = out['rounds'][-1] if out['rounds'] else None
        if run is not None:
            result = json.loads(run['result']) if run['result'] else {}
            out.update({'agent': run.get('agent'), 'model': run.get('model'),
                        'started_at': run['started_at'], 'ended_at': run.get('ended_at'),
                        'run_state': run['state'],
                        'outcome': result.get('outcome'),
                        'diagnosis': result.get('diagnosis'),
                        'actions': result.get('actions', []),
                        'evidence': result.get('evidence', [])})
        else:
            out.update({'agent': None, 'model': None, 'started_at': None,
                        'ended_at': None, 'run_state': None, 'outcome': None,
                        'diagnosis': None, 'actions': [], 'evidence': []})
        rows.append(out)
    return {'ok': True, 'total': total, 'rows': rows}


def _runtime_key(project):
    return 'runtime:' + json.dumps(project, ensure_ascii=False, separators=(',', ':'))


def _build_payload(db, key):
    if key == 'items':
        return items(db)
    if key == 'operator':
        return operator_ledger(db, {})
    if key.startswith('runtime:'):
        project = json.loads(key[len('runtime:'):])
        data = runtime(db, project)
        if data is None:
            raise KeyError('project removed during serving build: %s' % project)
        return data
    raise ValueError('unknown serving cache key')


def key_for_request(db, path, query, raw_query):
    """Return an exact finite key; extra, repeated or nondefault parameters miss."""
    if path in ('/api/items', '/api/operator') and not raw_query:
        return 'items' if path == '/api/items' else 'operator'
    if path != '/api/runtime' or not raw_query:
        return None
    pairs = parse_qsl(raw_query, keep_blank_values=True)
    if len(pairs) != 1 or pairs[0][0] != 'project' or not pairs[0][1]:
        return None
    project = pairs[0][1]
    if query.get('project') != project:
        return None
    known = db.execute('SELECT 1 FROM projects WHERE org_repo=?',
                       (project,)).fetchone()
    return _runtime_key(project) if known is not None else None


def lookup(db, key):
    """One read-only statement sees revision and payload from the same snapshot."""
    row = db.execute('''SELECT c.body,c.gzip_body FROM serving_cache c
        JOIN serving_meta m ON m.key='source_revision'
        WHERE c.key=? AND c.source_revision=CAST(m.value AS INTEGER)
          AND c.format_version=?''', (key, FORMAT_VERSION)).fetchone()
    return (bytes(row['body']), bytes(row['gzip_body'])) if row else None


def source_revision(db):
    row = db.execute("SELECT value FROM serving_meta WHERE key='source_revision'").fetchone()
    return int(row['value']) if row else 0


def precompute(db_path, build_seconds=DEFAULT_BUILD_SECONDS,
               batch_bytes=DEFAULT_BATCH_BYTES, before_publish=None):
    """Build a bounded snapshot, close it, then publish only at its revision."""
    from . import store as store_module

    started = time.monotonic()
    readonly = store.connect_readonly(db_path)
    try:
        readonly.execute('BEGIN')
        revision = source_revision(readonly)
        keys = ['items', 'operator'] + [
            _runtime_key(row['org_repo']) for row in readonly.execute(
                'SELECT org_repo FROM projects ORDER BY org_repo')]
        deadline = started + float(build_seconds)
        readonly.set_progress_handler(
            lambda: 1 if time.monotonic() > deadline else 0, 10000)
        built = {}
        failed = []
        byte_count = 0
        for index, key in enumerate(keys):
            if time.monotonic() > deadline or byte_count >= batch_bytes:
                failed.extend(keys[index:])
                break
            try:
                payload = _build_payload(readonly, key)
                body = json.dumps(payload, ensure_ascii=False,
                                  default=str).encode('utf-8')
                compressed = gzip.compress(body, mtime=0)
                size = len(body) + len(compressed)
                if byte_count + size > batch_bytes:
                    failed.append(key)
                    failed.extend(keys[index + 1:])
                    break
                built[key] = (body, compressed)
                byte_count += size
            except Exception as exc:
                failed.append(key)
                print('dashboard serving precompute failed for %s: %r' % (key, exc))
        readonly.commit()
    finally:
        readonly.close()

    if before_publish is not None:
        before_publish(revision)
    target = store_module.connect(db_path)
    try:
        target.execute('BEGIN IMMEDIATE')
        if source_revision(target) != revision:
            target.rollback()
            return {'built': 0, 'failed': failed, 'discarded': 'source changed',
                    'bytes': byte_count}
        keep = sorted(built)
        if keep:
            marks = ','.join('?' for _ in keep)
            target.execute('DELETE FROM serving_cache WHERE key NOT IN (%s)' % marks,
                           keep)
        else:
            target.execute('DELETE FROM serving_cache')
        for key in failed:
            target.execute('DELETE FROM serving_cache WHERE key=?', (key,))
        for key, (body, compressed) in built.items():
            old = target.execute(
                'SELECT body,gzip_body,source_revision,format_version '
                'FROM serving_cache WHERE key=?', (key,)).fetchone()
            if old is not None and bytes(old['body']) == body \
                    and bytes(old['gzip_body']) == compressed:
                if old['source_revision'] != revision \
                        or old['format_version'] != FORMAT_VERSION:
                    target.execute('UPDATE serving_cache SET source_revision=?, '
                                   'format_version=? WHERE key=?',
                                   (revision, FORMAT_VERSION, key))
            else:
                target.execute('''INSERT INTO serving_cache
                    (key,body,gzip_body,source_revision,format_version)
                    VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                    body=excluded.body,gzip_body=excluded.gzip_body,
                    source_revision=excluded.source_revision,
                    format_version=excluded.format_version''',
                    (key, body, compressed, revision, FORMAT_VERSION))
        target.commit()
    except BaseException:
        target.rollback()
        raise
    finally:
        target.close()
    return {'built': len(built), 'failed': failed, 'bytes': byte_count}
