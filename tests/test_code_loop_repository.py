from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "code-loop"


class CodeLoopRepositoryTests(unittest.TestCase):
    def test_code_loop_has_required_files(self) -> None:
        required = {
            SKILL_DIR / "SKILL.md",
            SKILL_DIR / "README.md",
            SKILL_DIR / "references" / "control-plane.md",
            SKILL_DIR / "references" / "reviewer.md",
            SKILL_DIR / "references" / "handler.md",
            SKILL_DIR / "references" / "discover.md",
            SKILL_DIR / "references" / "operator.md",
            SKILL_DIR / "scripts" / "_shim.py",
            SKILL_DIR / "scripts" / "control_plane.py",
            SKILL_DIR / "scripts" / "cp_server.py",
            SKILL_DIR / "scripts" / "dashboard.py",
            SKILL_DIR / "scripts" / "worker_agent.py",
            SKILL_DIR / "scripts" / "trigger.py",
            SKILL_DIR / "scripts" / "operator.py",
            SKILL_DIR / "scripts" / "check_ledger.py",
            SKILL_DIR / "scripts" / "test_shims.py",
        }
        missing = [str(p.relative_to(REPO_ROOT)) for p in required if not p.is_file()]
        self.assertEqual([], missing, f"missing required files: {missing}")

    def test_repository_indexes_the_skill(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        claude = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")

        self.assertIn("`code-loop`", readme)
        self.assertIn("--skill code-loop", readme)
        self.assertIn("### code-loop", claude)

    def test_shims_unit_tests_pass(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SKILL_DIR / "scripts" / "test_shims.py")],
            cwd=SKILL_DIR,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            0,
            result.returncode,
            f"shims test suite failed:\n{result.stdout}\n{result.stderr}",
        )

    def test_symlink_compatibility(self) -> None:
        symlink = REPO_ROOT / "skills" / "code-loop"
        if symlink.is_symlink():
            self.assertEqual(symlink.resolve(), SKILL_DIR.resolve())


if __name__ == "__main__":
    unittest.main()
