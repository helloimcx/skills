"""code-loop dashboard CLI.

    python scripts/dashboard.py collect [--db PATH] [--root PATH] [--project NAME]...
    python scripts/dashboard.py serve   [--db PATH] [--host HOST] [--port N]
                                        [--interval SECONDS]

`collect` merges every project under ~/.code-loop/projects/ into the global
dashboard SQLite (default ~/.code-loop/dashboard.sqlite); a non-zero exit means
at least one project failed (the failure is also recorded on its project row).
`serve` is a read-only HTTP facade for the app; --interval optionally enables a
background collect loop while serving.
"""
import argparse
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
if str(SERVICE) not in sys.path:
    sys.path.insert(0, str(SERVICE))

from codeloop import trigger  # noqa: E402  — spawn argv default for register
from codeloop.dash import collect, notify, serve, serving, store  # noqa: E402
from codeloop.dash.collect_lock import DatabaseRoundLock  # noqa: E402
from codeloop.operator import config as operator_config  # noqa: E402
from codeloop.operator import ingest as operator_ingest  # noqa: E402
from codeloop.operator import store as operator_store  # noqa: E402

DEFAULT_CONFIG = Path.home() / '.code-loop' / 'dashboard.json'


def load_config(path):
    """Optional dashboard config: {host, port, alerts:{...}}; unknown keys ignored."""
    if not Path(path).is_file():
        return {}
    try:
        raw = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        print('dashboard config %s unreadable: %s' % (path, exc), file=sys.stderr)
        return {}
    return raw if isinstance(raw, dict) else {}


def register_trigger(db, project, repo=None, entry_doc=None, rules_doc=None,
                     command=None, coordinator_agent=None,
                     coordinator_model=None, listing=False):
    """Register or edit one project's manual review-trigger facts on the
    projects table (see store.set_project_trigger). Repeatable: provided
    flags update, omitted keys keep their stored value; first registration
    derives entry_doc from the repo convention, falls back to the in-repo
    skill rules doc, and defaults the spawn command. Returns (exit_code,
    human-readable summary)."""
    if listing:
        rows = db.execute(
            'SELECT org_repo, enabled, repo_cwd, entry_doc, rules_doc, '
            'spawn_command,coordinator_agent,coordinator_model '
            'FROM projects ORDER BY org_repo').fetchall()
        if not rows:
            return 0, 'no projects known to the dashboard yet'
        lines = []
        for r in rows:
            lines.append('%s  (enabled=%s)\n  repo_cwd=%s\n  entry_doc=%s\n'
                         '  rules_doc=%s\n  command=%s\n  coordinator=%s model=%s'
                         % (r['org_repo'], bool(r['enabled']),
                            r['repo_cwd'] or '—', r['entry_doc'] or '—',
                            r['rules_doc'] or '—', r['spawn_command'] or '—',
                            r['coordinator_agent'] or '—',
                            r['coordinator_model'] or 'default'))
        return 0, '\n'.join(lines)

    fields = {}
    if repo is not None:
        if not Path(repo).is_dir():
            return 1, 'REPO_NOT_FOUND: %s' % repo
        fields['repo_cwd'] = str(Path(repo))
    for name, value in (('entry_doc', entry_doc), ('rules_doc', rules_doc)):
        if value is not None:
            if not Path(value).is_file():
                return 1, '%s_NOT_FOUND: %s' % (name.upper(), value)
            fields[name] = str(value)
    if command is not None:
        argv = [part.strip() for part in command.split(',')]
        if not all(argv):
            return 1, 'COMMAND_INVALID: empty argv part'
        fields['spawn_command'] = argv
    if coordinator_agent is not None:
        fields['coordinator_agent'] = coordinator_agent
    if coordinator_model is not None:
        fields['coordinator_model'] = coordinator_model

    row = db.execute('SELECT repo_cwd, entry_doc, rules_doc, spawn_command, '
                     'coordinator_agent,coordinator_model '
                     'FROM projects WHERE org_repo=?', (project,)).fetchone()
    current = {k: (row[k] if row is not None else None) for k in
               ('repo_cwd', 'entry_doc', 'rules_doc', 'spawn_command',
                'coordinator_agent', 'coordinator_model')}
    repo_cwd = fields.get('repo_cwd') or current['repo_cwd']
    if not repo_cwd:
        return 1, 'REPO_REQUIRED: --repo is required for the first registration'
    if 'entry_doc' not in fields and not current['entry_doc']:
        derived = (Path(repo_cwd) / '.agents' / 'loops' / 'reviewer' / 'prompt.md')
        if not derived.is_file():
            return 1, ('ENTRY_DOC_MISSING: %s not found — pass --entry-doc '
                       'explicitly' % derived)
        fields['entry_doc'] = str(derived)
    if 'rules_doc' not in fields and not current['rules_doc']:
        derived = (Path(repo_cwd) / 'skills' / 'code-loop' / 'references'
                   / 'reviewer.md')
        if derived.is_file():
            fields['rules_doc'] = str(derived)
    if 'spawn_command' not in fields and not current['spawn_command']:
        fields['spawn_command'] = list(trigger.DEFAULT_COMMAND)
    try:
        store.set_project_trigger(db, project, **fields)
    except ValueError as exc:
        return 1, str(exc)
    row = db.execute('SELECT repo_cwd, entry_doc, rules_doc, spawn_command, '
                     'coordinator_agent,coordinator_model '
                     'FROM projects WHERE org_repo=?', (project,)).fetchone()
    return 0, ('registered %s\n  repo_cwd=%s\n  entry_doc=%s\n  rules_doc=%s\n'
               '  command=%s\n  coordinator=%s model=%s'
               % (project, row['repo_cwd'], row['entry_doc'], row['rules_doc'],
                  row['spawn_command'], row['coordinator_agent'] or '—',
                  row['coordinator_model'] or 'default'))


def _open_operator_store(op_cfg):
    """Open the operator store for this evaluation point, or None when the
    subscription is disabled/unavailable. MINOR-3: a broken/absent store
    settles to None instead of raising — the scheduled collect carries on
    without the operator subscription, like cp_server's fail-closed wiring."""
    try:
        if op_cfg and op_cfg.get('enabled'):
            return operator_store.open_store()
    except Exception as exc:
        # A corrupt store raises at connect (sqlite3.DatabaseError); isolate
        # here so the sweep never dies on a stale state dir.
        print(json.dumps({'operator_store': 'unavailable',
                          'error': str(exc)}, ensure_ascii=False))
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(prog='dashboard', description=__doc__.splitlines()[1])
    sub = parser.add_subparsers(dest='command', required=True)

    p_collect = sub.add_parser('collect', help='collect all projects into the global store')
    p_collect.add_argument('--db', default=str(collect.DEFAULT_DB))
    p_collect.add_argument('--root', default=str(collect.DEFAULT_ROOT))
    p_collect.add_argument('--project', action='append', default=[],
                           help='restrict to org__repo (repeatable)')
    p_collect.add_argument('--config', default=str(DEFAULT_CONFIG),
                           help='dashboard config used for alerts and Operator ingest')
    p_collect.add_argument('--timeout', type=int, default=300,
                           help='hard child deadline in seconds (default: 300)')
    p_collect.add_argument('--result-file',
                           help='atomically write a bounded machine-readable result')

    p_serve = sub.add_parser('serve', help='serve the read-only dashboard')
    p_serve.add_argument('--db', default=str(collect.DEFAULT_DB))
    p_serve.add_argument('--host', default=serve.DEFAULT_HOST)
    p_serve.add_argument('--port', type=int, default=serve.DEFAULT_PORT)
    p_serve.add_argument('--interval', type=int, default=0, metavar='SECONDS',
                         help='background collect cadence while serving (default: off)')

    p_reg = sub.add_parser('register',
                           help='register/edit a project\'s manual review-trigger facts')
    p_reg.add_argument('--project', help='org__repo, e.g. mocx__skills')
    p_reg.add_argument('--repo', help='local checkout of the project (required once)')
    p_reg.add_argument('--entry-doc', help='reviewer entry doc (default: the '
                       'repo convention .agents/loops/reviewer/prompt.md)')
    p_reg.add_argument('--rules-doc', help='reviewer rules doc (default: the '
                       'in-repo skill copy skills/code-loop/references/reviewer.md)')
    p_reg.add_argument('--command', dest='spawn_argv',
                       help='spawn argv, comma-separated '
                       '(default: claude,-p,--dangerously-skip-permissions)')
    p_reg.add_argument('--coordinator-agent',
                       help='worker-local coordinator runtime id (claude/opencode)')
    p_reg.add_argument('--coordinator-model',
                       help='provider/model for Coordinator and child agent')
    p_reg.add_argument('--list', action='store_true', help='show registrations')
    p_reg.add_argument('--db', default=str(collect.DEFAULT_DB))

    args = parser.parse_args(argv)

    if args.command == 'collect':
        if not 1 <= args.timeout <= 3600:
            parser.error('--timeout must be between 1 and 3600 seconds')
        timer = threading.Timer(args.timeout, lambda: os._exit(124))
        timer.daemon = True
        timer.start()
        try:
            lock = DatabaseRoundLock(args.db)
            if not lock.acquire():
                result = {'collect': {'skipped': 'collect in progress'},
                          'notify': {'skipped': 'collect in progress'}}
                code = 0
            else:
                try:
                    config = load_config(args.config)
                    db = store.connect(args.db)
                    # Operator subscription (2026-09-30): consume fresh alerts
                    # at this evaluation point. The serve process alone owns
                    # the watchdog cursor.
                    op_cfg = operator_config.load_operator_config(config) \
                        if config else None
                    op_store = _open_operator_store(op_cfg)
                    try:
                        summary = collect.collect(db, root=args.root,
                                                  only=args.project or None,
                                                  db_path=args.db)
                        on_alerts = (lambda fresh: operator_ingest.ingest_alerts(
                            op_store, op_cfg, fresh)) if op_store is not None else None
                        note = notify.push_new_alerts(db, config, on_alerts=on_alerts)
                        result = {'collect': summary, 'notify': note}
                    finally:
                        if op_store is not None:
                            op_store.db.close()
                        db.close()
                    if args.db == ':memory:':
                        result['precompute'] = {'skipped': 'in-memory database'}
                    else:
                        try:
                            result['precompute'] = serving.precompute(args.db)
                        except Exception as exc:
                            # The read model is disposable. Collection/notification
                            # remain successful and HTTP falls back to live reads.
                            result['precompute'] = {'error': str(exc)}
                    code = 1 if summary['failed'] else 0
                finally:
                    lock.close()
        finally:
            timer.cancel()
        if args.result_file:
            encoded = json.dumps(result, ensure_ascii=False,
                                 separators=(',', ':')).encode('utf-8')
            if len(encoded) > 1024 * 1024:
                print('collect result exceeds 1 MiB', file=sys.stderr)
                return 2
            result_path = Path(args.result_file)
            result_path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='.dashboard-result-',
                                             dir=str(result_path.parent))
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, result_path)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
            print(json.dumps({'result_file_written': True, 'exit_code': code}))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return code

    if args.command == 'register':
        if not args.list and not args.project:
            parser.error('register requires --project (or --list)')
        db = store.connect(args.db)
        try:
            code, message = register_trigger(
                db, args.project or '', repo=args.repo, entry_doc=args.entry_doc,
                rules_doc=args.rules_doc, command=args.spawn_argv,
                coordinator_agent=args.coordinator_agent,
                coordinator_model=args.coordinator_model, listing=args.list)
        finally:
            db.close()
        print(message)
        return code

    if args.command == 'serve':
        # Retired: the dashboard UI is served by the merged CP server
        # (cp_server.py serve) on the same loopback port — one runtime, two
        # faces. This stub only points at it; running two servers would fight
        # over the port and over the same sqlite mirrors.
        print(json.dumps({
            'ok': False, 'code': 'DASHBOARD_SERVE_MERGED',
            'message': 'dashboard serve 已并入 cp_server（同一进程、同一 8712 端口）：'
                       'python services/code-loop/scripts/cp_server.py serve'},
            ensure_ascii=False))
        return 2
    return 1


if __name__ == '__main__':
    sys.exit(main())
