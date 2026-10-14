from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "prototype-first"


class PrototypeFirstRepositoryTests(unittest.TestCase):
    def test_prototype_first_has_required_files(self) -> None:
        required = {
            SKILL_DIR / "SKILL.md",
            SKILL_DIR / "references" / "conventions.md",
            SKILL_DIR / "references" / "template-README.md",
            SKILL_DIR / "references" / "template-page.html",
            SKILL_DIR / "scripts" / "check_design.py",
            SKILL_DIR / "scripts" / "test_check_design.py",
        }
        missing = [str(p.relative_to(REPO_ROOT)) for p in required if not p.is_file()]
        self.assertEqual([], missing, f"missing required files: {missing}")

    def test_repository_indexes_the_skill(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        claude = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")

        self.assertIn("`prototype-first`", readme)
        self.assertIn("--skill prototype-first", readme)
        self.assertIn("### prototype-first", claude)

    def test_check_design_tests_pass(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SKILL_DIR / "scripts" / "test_check_design.py")],
            cwd=SKILL_DIR,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            0,
            result.returncode,
            f"check_design test suite failed:\n{result.stdout}\n{result.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
