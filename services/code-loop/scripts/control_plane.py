#!/usr/bin/env python3
"""JSON CLI for the code-loop Control Plane.

One CLI, one verb surface, on every machine: the loop-facing verbs behave
identically regardless of where coordination lives. The config decides the
carrier and `service_for` is the only place that knows the difference — pass
a project config (--config) for the in-process coordinator personality, or
omit --config to run as a worker against this machine's global CP pointer
(~/.code-loop/control-plane.json, project derived from the working
directory's git origin). Terminal states are derived by the coordinator
(report + clean outbox ⇒ SUCCEEDED; sweep/steal/reconcile close the rest) —
there is no finish, heartbeat, or recover verb. Operator verbs
(enable/disable/reconcile) act on the machine holding the coordination
state; elsewhere they fail closed and point there.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

SERVICE = Path(__file__).resolve().parents[1]
if str(SERVICE) not in sys.path:
    sys.path.insert(0, str(SERVICE))

from codeloop.cp.runtime import load_config, load_global_worker_config, worker_global_path
from codeloop.cp.service import Service


def parser():
    p = argparse.ArgumentParser(prog="control_plane.py")
    p.add_argument("--config", help="project JSON configuration; omit to run as a "
                                    "worker via the machine-global CP config")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("sync", "status", "list", "enable", "disable"):
        q = sub.add_parser(name)
        if name == "list":
            q.add_argument("--kind")
            q.add_argument("--all", action="store_true",
                           help="return every work item (default: active states "
                                "READY/RUNNING/BLOCKED only, payload stripped — "
                                "issue #158, user decision 1A)")
    q = sub.add_parser("enroll")
    q.add_argument("--url", required=True, help="CP server base URL, e.g. https://cp.lan:8722")
    q.add_argument("--code", help="one-time enrollment code from the server (omit to pair: "
                                  "join → operator approves → poll for the token)")
    q.add_argument("--name", help="machine name (pairing mode; default: hostname)")
    q.add_argument("--pin", help="expected server certificate sha256 fingerprint")
    q.add_argument("--interpreter", help="absolute python for {python} verify placeholder "
                                         "(written into the machine-global config)")
    q.add_argument("--verify-timeout", type=int,
                   help="verify timeout seconds (written into the machine-global config)")
    q.add_argument("--worktree-root", help="worktree root (written into the machine-global config)")
    q = sub.add_parser("request-project")
    q.add_argument("--project", help="project key (default: derive from the working "
                                     "directory's git origin); proposes a grant — an "
                                     "operator still approves it server-side")
    q = sub.add_parser("claim")
    q.add_argument("--loop", required=True, choices=("discover", "handler", "reviewer"))
    q.add_argument("--worker", help="session label for this claim; default: derive it — "
                                    "<hostname>/<loop> locally, bare <loop> under a remote "
                                    "carrier (the server prefixes the enrolled machine name)")
    q.add_argument("--item", help="full item id or key (e.g. issue:334); a wrong "
                                  "selector is a loud ITEM_UNKNOWN, never an empty queue")
    q = sub.add_parser("bind")
    q.add_argument("--execution", required=True)
    q.add_argument("--token", required=True, type=int)
    q.add_argument("--worktree", required=True)
    q.add_argument("--branch")
    q = sub.add_parser("verify")
    q.add_argument("--execution", required=True)
    q.add_argument("--token", required=True, type=int)
    q = sub.add_parser("report")
    q.add_argument("--execution", required=True)
    q.add_argument("--token", required=True, type=int)
    q.add_argument("--file", required=True)
    q = sub.add_parser("publish")
    q.add_argument("--execution", required=True)
    q.add_argument("--token", required=True, type=int)
    q = sub.add_parser("reconcile")
    q.add_argument("--operation", required=True)
    q.add_argument("--receipt-file", required=True)
    sub.add_parser("doctor")
    return p


def service_for(config):
    """Single personality split: the carrier (in-process store vs CP server)
    is a property of the config, never of the CLI."""
    if config.get("mode") == "worker":
        from codeloop.cp.worker import WorkerService
        return WorkerService(config)
    return Service(config)


def execute(args):
    pre = _pre_config_command(args)
    if pre is not None:
        return pre
    if args.config:
        config = load_config(args.config, cwd=os.getcwd())
    else:
        config = load_global_worker_config(cwd=os.getcwd())
    service = service_for(config)
    return _service_command(service, args)


def _pre_config_command(args):
    """Commands that run before a project config exists: enrollment and
    self-checks only need the worker-global pointer, not a project."""
    if args.command == "enroll":
        # No config to load yet: enrollment creates the global `server` pointer.
        from codeloop.cp.client import enroll_machine, enroll_pairing
        if args.code:
            return enroll_machine(args.url, args.code, expected_pin=args.pin,
                                  config_path=args.config or worker_global_path())
        return enroll_pairing(args.url, args.name,
                              config_path=args.config or worker_global_path(),
                              interpreter=args.interpreter,
                              verify_timeout=args.verify_timeout,
                              worktree_root=args.worktree_root,
                              poll_seconds=5, poll_timeout=900)
    if args.command == "request-project":
        # 授权提案：项目键默认从 cwd git origin 推导（与运行时同源），批准在服务端。
        from codeloop.cp.client import Client
        project = args.project
        if not project:
            try:
                from codeloop.cp.runtime import _project_key_from_origin
                project = _project_key_from_origin(os.getcwd())
            except ValueError as exc:
                if str(exc).startswith('GIT_FAILED'):
                    raise ValueError(
                        'PROJECT_KEY_UNRESOLVED: %s has no usable git origin '
                        '(%s) — pass --project' % (os.getcwd(), exc)) from None
                raise
        path = Path(args.config or worker_global_path())
        if not path.is_file():
            raise ValueError('GLOBAL_WORKER_CONFIG_MISSING: %s (run enroll first)' % path)
        cfg = json.loads(path.read_text(encoding='utf-8'))
        return Client(cfg['server']).request_project(project)
    if args.command == "doctor":
        # Self-check needs the config to inspect, not to run against.
        from codeloop.cp.client import doctor
        return doctor(args.config or worker_global_path())
    return None


def _service_command(service, args):
    if args.command == "sync":
        return service.sync()
    if args.command == "status":
        return service.status()
    if args.command == "list":
        return {"items": _active_items(service, args)}
    if args.command == "enable":
        return service.enable()
    if args.command == "disable":
        return service.disable()
    if args.command == "claim":
        raw = service.claim_raw(args.loop, args.worker, args.item)
        if raw.get("execution") is not None:
            return raw["execution"]
        # 空队列（无持有者）保持裸 null——reviewer/handler 的「result:
        # null=空轮」判定契约不变（reviewer.md §claim）。仅槽满/资源冲突
        # 被持有者阻塞时才返回信封：BLOCKED 轮须报告谁占到几点。
        if not raw.get("blocked_by"):
            return None
        return {"execution": None, "blocked_by": raw.get("blocked_by")}
    if args.command == "bind":
        return service.bind(args.execution, args.token, args.worktree, args.branch)
    if args.command == "verify":
        return service.verify(args.execution, args.token)
    if args.command == "report":
        result = json.loads(Path(args.file).read_text(encoding="utf-8"))
        return service.report(args.execution, args.token, result)
    if args.command == "publish":
        return service.publish(args.execution, args.token)
    if args.command == "reconcile":
        receipt = json.loads(Path(args.receipt_file).read_text(encoding="utf-8"))
        return service.reconcile(args.operation, receipt)
    raise ValueError("UNKNOWN_COMMAND")


def _active_items(service, args):
    items = service.items(args.kind)
    if not args.all:
        # issue #158（用户决策 1A）：默认只回活动态，剥离 payload——
        # 历史终态项占 99%+ 且带完整 payload，全量回包曾达 16MB。
        items = [i for i in items if i['state'] in ('READY', 'RUNNING', 'BLOCKED')]
        for item in items:
            item.pop('payload', None)
    return items


def main(argv=None):
    try:
        result = execute(parser().parse_args(argv))
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "code": type(exc).__name__, "message": str(exc)},
                         ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
