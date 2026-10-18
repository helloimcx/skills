# Stdlib operator collision guard
if __name__ == 'operator':
    import _operator as _std_op
    for _k, _v in _std_op.__dict__.items():
        if not _k.startswith('__'):
            globals()[_k] = _v
else:
    """Session-side CLI for operator rounds (the stable entry the prompt names).

    Machine-local by default: it reads the operator store directly — no serve
    dependency for status/report. For acts, the split is structural (review
    MAJOR-3): verbs that need the serve process's executor (collect_refresh /
    command_resend — they ride the dispatcher and the collect refresh closure
    wired into cp_server) cannot run in a detached session, so the CLI forwards
    them to the loopback data plane (`POST /api/v1/operator/act`, machine token
    fencing on both sides, executor runs serve-side). The local-only verbs
    (no_op / sweep_expired) run in-session against the store. Unwired verbs fail
    honestly with OPERATOR_VERB_NOT_WIRED.
      status                                signals, live rounds, recent decisions
      act     --execution E --token T --verb V [--target S] [--detail S]
      report  --execution E --token T --file report.json
    """
    import argparse
    import json
    import socket
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from codeloop.operator import face, store as op_store  # noqa: E402

    LOCAL_VERBS = {'no_op', 'sweep_expired'}


    def _worker():
        return socket.gethostname() + '/operator'


    def cmd_status(_args):
        store = op_store.open_store()
        try:
            live = [dict(r) for r in store.db.execute(
                "SELECT e.id,e.item_id,e.worker,e.state,e.expires_at,i.key "
                "FROM executions e JOIN work_items i ON i.id=e.item_id "
                "WHERE e.state IN ('RUNNING','VERIFYING') ORDER BY e.created_at")]
            ready = [dict(r) for r in store.db.execute(
                "SELECT id,key,payload,created_at FROM work_items WHERE state='READY' "
                "AND kind='operator' ORDER BY created_at")]
            commands = [dict(r) for r in store.db.execute(
                "SELECT id,action,number,state,claimed_by,execution,agent,model "
                "FROM commands ORDER BY id DESC LIMIT 20")]
            print(json.dumps({'worker': _worker(), 'live_rounds': live,
                              'ready_signals': ready, 'commands': commands},
                             ensure_ascii=False, indent=2))
        finally:
            store.db.close()


    def _data_plane_config():
        """The machine-global CP server config naming the loopback data plane
        (the same ~/.code-loop/control-plane.json the worker CLI reads). None — and
        thus OPERATOR_DATA_PLANE_UNREACHABLE — when absent or unreadable."""
        path = Path.home() / '.code-loop' / 'control-plane.json'
        try:
            cfg = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        return cfg if isinstance(cfg, dict) and isinstance(cfg.get('server'), dict) else None


    def _data_plane_act(verb, assignment=None, config=None):
        """Forward one wired act to the serve-side executor via the loopback data
        plane (review MAJOR-3) — the session process has no executor; the serve
        process wired these verbs and owns the dispatcher/refresh they need."""
        from codeloop.cp.client import Client
        cfg = config if config is not None else _data_plane_config()
        if cfg is None:
            raise ValueError('OPERATOR_DATA_PLANE_UNREACHABLE: no CP server config '
                             'under ~/.code-loop/control-plane.json')
        result = Client(cfg['server']).call(
            'POST', '/api/v1/operator/act',
            {'verb': verb, 'execution': assignment.get('execution'),
             'token': assignment.get('token'), 'target': assignment.get('target', ''),
             'detail': assignment.get('detail', '')})
        return result.get('receipt', result)


    def _act_local(store, args):
        """Run a local-only verb in-process against the operator store."""
        try:
            receipt = face.act(store, args.execution, args.token, args.verb,
                               target=args.target or '', detail=args.detail or '')
        except ValueError as exc:
            print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps({'ok': True, 'verb': args.verb, 'receipt': receipt},
                         ensure_ascii=False, indent=2))
        return 0


    def cmd_act(args, data_plane=None):
        """One L1 verb: local-only in-process; the rest via the loopback data
        plane so the serve-side executor actually runs them (review MAJOR-3)."""
        if args.verb in LOCAL_VERBS:
            store = op_store.open_store()
            try:
                return _act_local(store, args)
            finally:
                store.db.close()
        fn = data_plane or _data_plane_act
        try:
            receipt = fn(args.verb, {'execution': args.execution, 'token': args.token,
                                     'target': args.target or '', 'detail': args.detail or ''})
        except ValueError as exc:
            print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps({'ok': True, 'verb': args.verb, 'receipt': receipt},
                         ensure_ascii=False, indent=2))
        return 0


    def cmd_report(args):
        result = json.loads(Path(args.file).read_text(encoding='utf-8'))
        store = op_store.open_store()
        try:
            face.report(store, args.execution, args.token, result)
            print(json.dumps({'ok': True, 'outcome': result.get('outcome')},
                             ensure_ascii=False))
        except ValueError as exc:
            print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
            return 1
        finally:
            store.db.close()
        return 0


    def main(argv=None):
        parser = argparse.ArgumentParser(prog='operator.py', description=__doc__)
        sub = parser.add_subparsers(dest='command', required=True)
        sub.add_parser('status')
        p_act = sub.add_parser('act')
        p_act.add_argument('--execution', required=True)
        p_act.add_argument('--token', type=int, required=True)
        p_act.add_argument('--verb', required=True)
        p_act.add_argument('--target', default='')
        p_act.add_argument('--detail', default='')
        p_rep = sub.add_parser('report')
        p_rep.add_argument('--execution', required=True)
        p_rep.add_argument('--token', type=int, required=True)
        p_rep.add_argument('--file', required=True)
        args = parser.parse_args(argv)
        if args.command == 'status':
            cmd_status(args)
            return 0
        if args.command == 'act':
            return cmd_act(args)
        return cmd_report(args)


    if __name__ == '__main__':
        sys.exit(main())
