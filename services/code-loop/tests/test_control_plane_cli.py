#!/usr/bin/env python3
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import sys as _sys
from pathlib import Path as _Path

for _p in (str(_Path(__file__).resolve().parents[1]),
           str(_Path(__file__).resolve().parents[1] / "scripts")):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)


class CliTests(unittest.TestCase):
    def test_status_enable_claim_gating(self):
        # The CLI derives the repo from its working directory: run inside this repo.
        repo = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "control-plane.json"
            config.write_text(json.dumps({
                "state_dir": str(root / "state"), "worktree_root": str(root / "trees"),
                "base": "main", "platform": {"url": "http://127.0.0.1:1", "repo": "a/b"},
            }), encoding="utf-8")
            script = Path(__file__).resolve().parents[1] / "scripts" / "control_plane.py"
            cmd = [sys.executable, str(script), "--config", str(config)]

            def run(*args):
                # Redirect the machine-global observer store: the CLI's sync
                # path touches it even when the crawl itself fails.
                return subprocess.run(cmd + list(args), capture_output=True, text=True,
                                      cwd=str(repo),
                                      env=dict(os.environ, CODE_LOOP_HOME=str(root)))

            passed = run("status")
            self.assertEqual(passed.returncode, 0, passed.stderr)
            result = json.loads(passed.stdout)["result"]
            self.assertFalse(result["enabled"])
            refused = run("claim", "--loop", "discover", "--worker", "w")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("MACHINE_NOT_ENABLED", refused.stdout)
            self.assertEqual(run("enable").returncode, 0)
            self.assertTrue(json.loads(run("status").stdout)["result"]["enabled"])
            # Enabled but never synced: refusal reason changes to staleness.
            stale = run("claim", "--loop", "discover", "--worker", "w")
            self.assertNotEqual(stale.returncode, 0)
            self.assertNotIn("MACHINE_NOT_ENABLED", stale.stdout)
            self.assertEqual(run("disable").returncode, 0)
            self.assertIn("MACHINE_NOT_ENABLED", run("claim", "--loop",
                                                     "discover", "--worker", "w").stdout)
            broken = run("status", "--bad")
            self.assertNotEqual(broken.returncode, 0)

    def test_claim_without_worker_is_accepted(self):
        # --worker is optional: the CLI derives <hostname>/<loop> (remote
        # carriers send the bare loop as the suffix instead). Argparse exit 2
        # here would mean the flag is still mandatory.
        repo = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "control-plane.json"
            config.write_text(json.dumps({
                "state_dir": str(root / "state"), "worktree_root": str(root / "trees"),
                "base": "main", "platform": {"url": "http://127.0.0.1:1", "repo": "a/b"},
            }), encoding="utf-8")
            script = Path(__file__).resolve().parents[1] / "scripts" / "control_plane.py"
            proc = subprocess.run([sys.executable, str(script), "--config", str(config),
                                   "claim", "--loop", "discover"],
                                  capture_output=True, text=True, cwd=str(repo),
                                  env=dict(os.environ, CODE_LOOP_HOME=str(root)))
            self.assertNotEqual(2, proc.returncode, proc.stderr)
            self.assertIn("MACHINE_NOT_ENABLED", proc.stdout)


class _FakePlatform:
    """Enough platform for the offline seed: one empty snapshot, no writes."""

    def snapshot(self, known=None):
        return {"base_sha": "base1", "issues": [], "prs": []}

    def raw_file(self, path, ref):
        return None

    def pr_files(self, number):
        return []
    def find(self, marker, action, payload):
        return None

    def write(self, action, payload):
        raise AssertionError("the claim path must not write to the platform")


class ClaimDefaultWorkerTests(unittest.TestCase):
    """The real CLI against a real offline store: omitting --worker must land
    this host's <hostname>/<loop> label in the execution row."""

    def test_cli_records_the_derived_label(self):
        repo = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "control-plane.json"
            config.write_text(json.dumps({
                "state_dir": str(root / "state"), "worktree_root": str(root / "trees"),
                "base": "main", "platform": {"url": "http://127.0.0.1:1", "repo": "a/b"},
            }), encoding="utf-8")
            env = dict(os.environ, CODE_LOOP_HOME=str(root))
            # Seed enable + a fresh snapshot in-process: a fresh snapshot skips
            # claim's inline re-sync, so the CLI needs no network.
            previous = os.environ.get("CODE_LOOP_HOME")
            os.environ["CODE_LOOP_HOME"] = str(root)
            try:
                from codeloop.cp.runtime import load_config
                from codeloop.cp.service import Service
                seed = Service(load_config(str(config), cwd=str(repo)), _FakePlatform())
                seed.enable()
                seed.sync()
                seed.store.db.close()
            finally:
                if previous is None:
                    os.environ.pop("CODE_LOOP_HOME", None)
                else:
                    os.environ["CODE_LOOP_HOME"] = previous
            script = Path(__file__).resolve().parents[1] / "scripts" / "control_plane.py"
            proc = subprocess.run([sys.executable, str(script), "--config", str(config),
                                   "claim", "--loop", "discover"],
                                  capture_output=True, text=True, cwd=str(repo), env=env)
            self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
            db = sqlite3.connect(str(root / "state" / "control-plane.sqlite"))
            row = db.execute("SELECT worker FROM executions "
                             "ORDER BY created_at DESC LIMIT 1").fetchone()
            db.close()
            self.assertEqual(socket.gethostname().strip() + "/discover", row[0])


class ClaimBlockedByTests(unittest.TestCase):
    """A null claim carries the capacity holders: a loop session reporting
    BLOCKED must name who holds the slots until when (ai_vector, 2026-09-30),
    not a bare null that reads as "coordinator broken"."""

    def test_claim_null_output_carries_blocked_by(self):
        repo = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "control-plane.json"
            config.write_text(json.dumps({
                "state_dir": str(root / "state"), "worktree_root": str(root / "trees"),
                "base": "main", "platform": {"url": "http://127.0.0.1:1", "repo": "a/b"},
            }), encoding="utf-8")
            env = dict(os.environ, CODE_LOOP_HOME=str(root))
            previous = os.environ.get("CODE_LOOP_HOME")
            os.environ["CODE_LOOP_HOME"] = str(root)
            try:
                from codeloop.cp.runtime import load_config
                from codeloop.cp.service import Service
                seed = Service(load_config(str(config), cwd=str(repo)), _FakePlatform())
                seed.enable()
                seed.sync()
                for n in (1, 2, 3):
                    seed.store.put_item('issue-%d' % n, 'handler', 'v1',
                                        {'number': n}, ['issue-%d' % n])
                    seed.store.claim('handler', 'holder-%d' % n)
                seed.store.db.close()
            finally:
                if previous is None:
                    os.environ.pop("CODE_LOOP_HOME", None)
                else:
                    os.environ["CODE_LOOP_HOME"] = previous
            script = Path(__file__).resolve().parents[1] / "scripts" / "control_plane.py"
            proc = subprocess.run([sys.executable, str(script), "--config", str(config),
                                   "claim", "--loop", "handler", "--worker", "seeker"],
                                  capture_output=True, text=True, cwd=str(repo), env=env)
            self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
            result = json.loads(proc.stdout)["result"]
            self.assertIsNone(result["execution"])
            self.assertEqual({'holder-1', 'holder-2', 'holder-3'},
                             {b['worker'] for b in result['blocked_by']})

    def test_claim_null_output_is_bare_null_when_queue_empty(self):
        """review MAJOR-1: an empty queue keeps the contract 「result: null =
        空轮」— never an {execution:null, blocked_by:null} envelope, so loop
        callers short-circuit on a bare null exactly as before."""
        repo = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "control-plane.json"
            config.write_text(json.dumps({
                "state_dir": str(root / "state"), "worktree_root": str(root / "trees"),
                "base": "main", "platform": {"url": "http://127.0.0.1:1", "repo": "a/b"},
            }), encoding="utf-8")
            env = dict(os.environ, CODE_LOOP_HOME=str(root))
            previous = os.environ.get("CODE_LOOP_HOME")
            os.environ["CODE_LOOP_HOME"] = str(root)
            try:
                from codeloop.cp.runtime import load_config
                from codeloop.cp.service import Service
                seed = Service(load_config(str(config), cwd=str(repo)), _FakePlatform())
                seed.enable()
                seed.sync()
                seed.store.db.close()
            finally:
                if previous is None:
                    os.environ.pop("CODE_LOOP_HOME", None)
                else:
                    os.environ["CODE_LOOP_HOME"] = previous
            script = Path(__file__).resolve().parents[1] / "scripts" / "control_plane.py"
            proc = subprocess.run([sys.executable, str(script), "--config", str(config),
                                   "claim", "--loop", "handler", "--worker", "seeker"],
                                  capture_output=True, text=True, cwd=str(repo), env=env)
            self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
            self.assertIsNone(json.loads(proc.stdout)["result"])

    def test_claim_success_output_carries_no_result_field(self):
        """A successful claim must be unmistakable: the response object has
        id/token/item_id and NOT a `result` field. The executions-table
        `result` column (report payload) used to leak into this response
        always-null, and a loop session reading 「result: null」 off the
        object misread every claimed review as an empty round — dropping 14
        claimed PR reviews over 27h (ai_quality_analysis, 2026-10-08/09)."""
        repo = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "control-plane.json"
            config.write_text(json.dumps({
                "state_dir": str(root / "state"), "worktree_root": str(root / "trees"),
                "base": "main", "platform": {"url": "http://127.0.0.1:1", "repo": "a/b"},
            }), encoding="utf-8")
            env = dict(os.environ, CODE_LOOP_HOME=str(root))
            previous = os.environ.get("CODE_LOOP_HOME")
            os.environ["CODE_LOOP_HOME"] = str(root)
            try:
                from codeloop.cp.runtime import load_config
                from codeloop.cp.service import Service
                seed = Service(load_config(str(config), cwd=str(repo)), _FakePlatform())
                seed.enable()
                seed.sync()
                seed.store.put_item('pr-1', 'reviewer', 'v1', {'pr': 1}, ['pr-1'])
                seed.store.db.close()
            finally:
                if previous is None:
                    os.environ.pop("CODE_LOOP_HOME", None)
                else:
                    os.environ["CODE_LOOP_HOME"] = previous
            script = Path(__file__).resolve().parents[1] / "scripts" / "control_plane.py"
            proc = subprocess.run([sys.executable, str(script), "--config", str(config),
                                   "claim", "--loop", "reviewer", "--worker", "seeker"],
                                  capture_output=True, text=True, cwd=str(repo), env=env)
            self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
            execution = json.loads(proc.stdout)["result"]
            self.assertIsInstance(execution, dict)
            self.assertIn("id", execution)
            self.assertIn("token", execution)
            self.assertIn("item_id", execution)
            self.assertNotIn("result", execution)


class ListActiveTests(unittest.TestCase):
    """issue #158（用户决策 1A）：list 默认只回活动态且剥离 payload，
    --all 取全量。历史终态项带完整 payload 曾使单项目回包达 16MB。"""

    def test_list_defaults_to_active_states_without_payload(self):
        repo = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "control-plane.json"
            config.write_text(json.dumps({
                "state_dir": str(root / "state"), "worktree_root": str(root / "trees"),
                "base": "main", "platform": {"url": "http://127.0.0.1:1", "repo": "a/b"},
            }), encoding="utf-8")
            env = dict(os.environ, CODE_LOOP_HOME=str(root))
            previous = os.environ.get("CODE_LOOP_HOME")
            os.environ["CODE_LOOP_HOME"] = str(root)
            try:
                from codeloop.cp.runtime import load_config
                from codeloop.cp.service import Service
                seed = Service(load_config(str(config), cwd=str(repo)), _FakePlatform())
                seed.enable()
                seed.sync()
                # 一个活动态（READY）+ 一个终态（CANCELLED），都带 payload。
                seed.store.put_item('issue:1', 'handler', 'v1', {'number': 1}, ['issue-1'])
                seed.store.put_item('issue:2', 'handler', 'v2', {'number': 2}, ['issue-2'])
                seed.store.db.execute(
                    "UPDATE work_items SET state='CANCELLED' WHERE key='issue:2'")
                seed.store.db.commit()
                seed.store.db.close()
            finally:
                if previous is None:
                    os.environ.pop("CODE_LOOP_HOME", None)
                else:
                    os.environ["CODE_LOOP_HOME"] = previous
            script = Path(__file__).resolve().parents[1] / "scripts" / "control_plane.py"

            def run(*args):
                return subprocess.run([sys.executable, str(script), "--config", str(config),
                                       *args], capture_output=True, text=True,
                                      cwd=str(repo), env=env)

            default = json.loads(run("list").stdout)["result"]["items"]
            self.assertEqual(2, len(default), '默认只回活动态: %s' % default)
            self.assertTrue(all(i['state'] in ('READY', 'RUNNING', 'BLOCKED')
                                for i in default), '默认只含活动态')
            self.assertTrue(all('payload' not in i for i in default), '默认剥离 payload')
            all_items = json.loads(run("list", "--all").stdout)["result"]["items"]
            self.assertEqual(3, len(all_items))
            self.assertTrue(all('payload' in i for i in all_items), '--all 保留 payload')


class RetiredVerbTests(unittest.TestCase):
    def test_retired_verbs_are_unknown_cli_commands(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "control_plane.py"
        for verb, args in (("finish", ["--execution", "x", "--token", "1", "--state", "SUCCEEDED"]),
                           ("heartbeat", ["--execution", "x", "--token", "1"]),
                           ("recover", ["--execution", "x", "--evidence", "y"])):
            proc = subprocess.run([sys.executable, str(script), verb, *args],
                                  capture_output=True, text=True)
            self.assertNotEqual(0, proc.returncode, verb)
            self.assertIn("invalid choice", proc.stderr)


class EnrollPairingTests(unittest.TestCase):
    """Pairing enroll: --code is now optional — no --code means join → poll →
    consume. The transport is stubbed so these exercise CLI wiring, not HTTP."""

    def test_enroll_accepts_optional_code_and_pairing_fields(self):
        """No usage error when --code is omitted; pairing/field args parse."""
        script = Path(__file__).resolve().parents[1] / "scripts" / "control_plane.py"
        proc = subprocess.run(
            [sys.executable, str(script), "enroll", "--url", "https://cp.lan:8722",
             "--name", "gpu", "--interpreter", "/venv/bin/python",
             "--verify-timeout", "300"],
            capture_output=True, text=True)
        # 解析通过（不报 usage/required 错误）；之后会尝试连真实服务器并失败
        self.assertNotIn("required", proc.stderr)
        self.assertNotIn("invalid choice", proc.stderr)
        self.assertNotIn("usage:", proc.stderr)

    def test_doctor_reports_missing_config(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "control_plane.py"
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "nope.json"
            proc = subprocess.run([sys.executable, str(script), "--config", str(missing),
                                   "doctor"],
                                  capture_output=True, text=True)
            self.assertNotEqual(0, proc.returncode)
            self.assertIn("DOCTOR_NO_CONFIG", proc.stdout + proc.stderr)

    def test_enroll_rejects_project_flag(self):
        """注册与授权解耦：worker enroll 不再接受 --project（授权在服务端）。"""
        script = Path(__file__).resolve().parents[1] / "scripts" / "control_plane.py"
        proc = subprocess.run([sys.executable, str(script), "enroll",
                               "--url", "https://cp.lan:8722", "--project", "a__b"],
                              capture_output=True, text=True)
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("unrecognized arguments", proc.stderr)

    def test_enroll_bare_goes_to_connection_not_usage_error(self):
        """bare enroll 以 hostname 为机器名进入配对：失败是连接层，不是用法层。"""
        script = Path(__file__).resolve().parents[1] / "scripts" / "control_plane.py"
        proc = subprocess.run([sys.executable, str(script), "enroll",
                               "--url", "https://cp.lan:8722"],
                              capture_output=True, text=True)
        self.assertNotEqual(0, proc.returncode)
        self.assertNotIn("usage:", proc.stderr)
        self.assertNotIn("JOIN_PROJECTS_REQUIRED", proc.stdout + proc.stderr)


class RequestProjectCliTests(unittest.TestCase):
    """worker 发起的授权提案：项目键默认从 cwd git origin 推导。"""

    def setUp(self):
        self.script = Path(__file__).resolve().parents[1] / "scripts" / "control_plane.py"

    def run_cli(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(self.script), *args],
                              capture_output=True, text=True, cwd=cwd)

    def test_derivation_fails_loud_outside_git(self):
        with tempfile.TemporaryDirectory() as temp:
            proc = self.run_cli("request-project", cwd=temp)
            self.assertNotEqual(0, proc.returncode)
            self.assertIn("PROJECT_KEY_UNRESOLVED", proc.stdout + proc.stderr)

    def test_missing_global_config_is_loud(self):
        with tempfile.TemporaryDirectory() as temp:
            proc = self.run_cli("--config", str(Path(temp) / "nope.json"),
                                "request-project", "--project", "a__b")
            self.assertNotEqual(0, proc.returncode)
            self.assertIn("GLOBAL_WORKER_CONFIG_MISSING", proc.stdout + proc.stderr)


class DoctorVerifyTimeoutTests(unittest.TestCase):
    """issue #200：verify_timeout 双真相源——全局 projects.<key> override 与
    项目 config 各管一边时，doctor 必须 fail-loud 报不一致（不静默）。"""

    def _write(self, base, rel, data):
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        return p

    def test_mismatch_reported_as_failed_check(self):
        from codeloop.cp.client import doctor
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            # 全局 config：projects.a__b override verify_timeout=900
            self._write(root, 'control-plane.json', {
                'server': {'url': 'https://cp.lan:8722'},
                'projects': {'a__b': {'verify_timeout': 900}}})
            # 项目 config：verify_timeout=300（双真相）
            self._write(root, 'projects/a__b/control-plane.json',
                        {'verify_timeout': 300})
            result = doctor(str(root / 'control-plane.json'))
            names = {c['name'] for c in result['checks']}
            self.assertIn('verify_timeout.a__b', names)
            mismatch = next(c for c in result['checks']
                            if c['name'] == 'verify_timeout.a__b')
            self.assertFalse(mismatch['ok'])
            self.assertIn('300', mismatch['detail'])
            self.assertIn('900', mismatch['detail'])

    def test_consistent_values_no_warning(self):
        from codeloop.cp.client import doctor
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write(root, 'control-plane.json', {
                'server': {'url': 'https://cp.lan:8722'},
                'projects': {'a__b': {'verify_timeout': 600}}})
            self._write(root, 'projects/a__b/control-plane.json', {'verify_timeout': 600})
            result = doctor(str(root / 'control-plane.json'))
            self.assertNotIn('verify_timeout.a__b',
                             {c['name'] for c in result['checks']})

    def test_missing_project_config_skipped(self):
        from codeloop.cp.client import doctor
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._write(root, 'control-plane.json', {
                'server': {'url': 'https://cp.lan:8722'},
                'projects': {'a__b': {'verify_timeout': 900}}})
            # 项目 config 不存在：无从比较，不报（非错误）
            result = doctor(str(root / 'control-plane.json'))
            self.assertNotIn('verify_timeout.a__b',
                             {c['name'] for c in result['checks']})


if __name__ == "__main__":
    unittest.main()
