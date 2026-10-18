#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_shims.py — Unit tests for code-loop thin skill delegation shims."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_SCRIPTS = REPO_ROOT / "code-loop" / "scripts"
SERVICE_ROOT = REPO_ROOT / "services" / "code-loop"

if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

import _shim  # noqa: E402


class ShimDiscoveryTests(unittest.TestCase):
    def test_walkup_finds_real_service(self):
        """Walk-up from skill scripts correctly locates services/code-loop."""
        found = _shim.find_service_root(from_file=SKILL_SCRIPTS / "control_plane.py")
        self.assertEqual(found, SERVICE_ROOT.resolve())

    def test_env_var_takes_priority(self):
        """Valid CODE_LOOP_SERVICE environment variable is respected."""
        with tempfile.TemporaryDirectory() as temp:
            fake_svc = Path(temp) / "fake-svc"
            (fake_svc / "scripts").mkdir(parents=True)
            (fake_svc / "scripts" / "control_plane.py").write_text("# fake", encoding="utf-8")

            old_env = os.environ.get("CODE_LOOP_SERVICE")
            os.environ["CODE_LOOP_SERVICE"] = str(fake_svc)
            try:
                found = _shim.find_service_root(from_file=SKILL_SCRIPTS / "control_plane.py")
                self.assertEqual(found, fake_svc.resolve())
            finally:
                if old_env is None:
                    os.environ.pop("CODE_LOOP_SERVICE", None)
                else:
                    os.environ["CODE_LOOP_SERVICE"] = old_env

    def test_env_var_invalid_fails_closed(self):
        """Invalid CODE_LOOP_SERVICE exits 1 with CODE_LOOP_SERVICE_INVALID."""
        proc = subprocess.run(
            [sys.executable, str(SKILL_SCRIPTS / "control_plane.py"), "--help"],
            capture_output=True,
            text=True,
            env=dict(os.environ, CODE_LOOP_SERVICE="/non/existent/path"),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("CODE_LOOP_SERVICE_INVALID", proc.stderr)

    def test_pinned_home_valid(self):
        """Pinned service.home file is used if walkup fails."""
        with tempfile.TemporaryDirectory() as temp:
            isolated = Path(temp) / "isolated" / "scripts"
            isolated.mkdir(parents=True)
            shim_file = isolated / "control_plane.py"
            shim_file.write_text("# isolated", encoding="utf-8")

            fake_svc = Path(temp) / "fake-svc"
            (fake_svc / "scripts").mkdir(parents=True)
            (fake_svc / "scripts" / "control_plane.py").write_text("# marker", encoding="utf-8")

            fake_pin = Path(temp) / "service.home"
            fake_pin.write_text(str(fake_svc) + "\n", encoding="utf-8")

            orig_pin = _shim.DEFAULT_PIN
            _shim.DEFAULT_PIN = fake_pin
            try:
                found = _shim.find_service_root(from_file=shim_file)
                self.assertEqual(found, fake_svc.resolve())
            finally:
                _shim.DEFAULT_PIN = orig_pin

    def test_pinned_home_invalid_fails_closed(self):
        """Invalid pinned service.home fails with SERVICE_PIN_INVALID."""
        with tempfile.TemporaryDirectory() as temp:
            isolated = Path(temp) / "isolated" / "scripts"
            isolated.mkdir(parents=True)
            shim_file = isolated / "control_plane.py"
            shim_file.write_text("# isolated", encoding="utf-8")

            fake_pin = Path(temp) / "service.home"
            fake_pin.write_text("/invalid/path\n", encoding="utf-8")

            orig_pin = _shim.DEFAULT_PIN
            _shim.DEFAULT_PIN = fake_pin
            try:
                with self.assertRaises(SystemExit) as cm:
                    _shim.find_service_root(from_file=shim_file)
                self.assertEqual(cm.exception.code, 1)
            finally:
                _shim.DEFAULT_PIN = orig_pin

    def test_all_unmatched_fails_closed(self):
        """When neither env, walkup, nor pin matches, fail closed with SERVICE_NOT_FOUND."""
        with tempfile.TemporaryDirectory() as temp:
            isolated = Path(temp) / "isolated" / "scripts"
            isolated.mkdir(parents=True)
            shim_file = isolated / "control_plane.py"
            shim_file.write_text("# isolated", encoding="utf-8")

            orig_pin = _shim.DEFAULT_PIN
            _shim.DEFAULT_PIN = Path(temp) / "nonexistent.home"
            try:
                with self.assertRaises(SystemExit) as cm:
                    _shim.find_service_root(from_file=shim_file)
                self.assertEqual(cm.exception.code, 1)
            finally:
                _shim.DEFAULT_PIN = orig_pin


class ShimExecutionTests(unittest.TestCase):
    def test_all_shims_execute_help(self):
        """All shims in code-loop/scripts execute --help without error."""
        shims = [
            "control_plane.py",
            "dashboard.py",
            "trigger.py",
            "cp_server.py",
            "worker_agent.py",
            "operator.py",
            "check_ledger.py",
        ]
        for name in shims:
            script = SKILL_SCRIPTS / name
            proc = subprocess.run(
                [sys.executable, str(script), "--help"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"Shim {name} failed with code {proc.returncode}:\n{proc.stderr}",
            )


if __name__ == "__main__":
    unittest.main()
