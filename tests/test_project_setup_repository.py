from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "project-setup"


class ProjectSetupRepositoryTests(unittest.TestCase):
    def test_large_module_has_architecture_index_and_document(self) -> None:
        modules_index = REPO_ROOT / "docs" / "architecture" / "modules.md"
        module_document = (
            REPO_ROOT / "docs" / "architecture" / "modules" / "project-setup.md"
        )

        self.assertTrue(modules_index.is_file(), "missing module architecture index")
        self.assertTrue(module_document.is_file(), "missing project-setup module document")
        self.assertIn(
            "modules/project-setup.md",
            modules_index.read_text(encoding="utf-8"),
        )

    def test_repository_indexes_the_skill(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        claude = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")

        self.assertIn("`project-setup`", readme)
        self.assertIn("--skill project-setup", readme)
        self.assertIn("### project-setup", claude)

        project_setup_row = next(
            line for line in readme.splitlines() if line.startswith("| `project-setup`")
        )
        project_setup_section = claude.split("### project-setup", 1)[1].split("### ", 1)[0]
        self.assertNotIn("vibe-coding", project_setup_row.casefold())
        self.assertNotIn("vibe-coding", project_setup_section.casefold())

    def test_distributed_skill_contract_is_self_contained(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            installed_skill = Path(temp_dir) / "project-setup"
            shutil.copytree(
                SKILL_DIR,
                installed_skill,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    str(installed_skill / "tests"),
                    "-p",
                    "test_*.py",
                    "-v",
                ],
                cwd=installed_skill,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(
            0,
            result.returncode,
            f"installed skill contract failed:\n{result.stdout}\n{result.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
