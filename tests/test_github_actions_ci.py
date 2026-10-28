from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
GATE = REPO_ROOT / ".github" / "scripts" / "ci_gate.py"


class GitHubActionsContractTests(unittest.TestCase):
    def test_ci_workflow_runs_on_pull_request_main_and_manual_dispatch(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("pull_request:\n    types: [opened, synchronize, reopened]", workflow)
        self.assertIn("push:\n    branches: [main]", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("paths:", workflow)

    def test_ci_workflow_tests_repository_and_service_on_supported_baseline(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("python -m unittest discover -s tests -p 'test_*.py'", workflow)
        self.assertIn(
            "python -m unittest discover -s services/code-loop/tests -p 'test_*.py'",
            workflow,
        )
        self.assertIn("name: Repository tests (Python 3.9)", workflow)
        self.assertIn("name: Service tests (Python 3.9)", workflow)
        self.assertEqual(3, workflow.count("python-version: '3.9'"))
        self.assertNotIn("matrix:", workflow)
        self.assertNotIn("3.13", workflow)

    def test_ci_workflow_uses_hardened_actions_and_stable_final_gate(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn(
            "uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0",
            workflow,
        )
        self.assertIn(
            "uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0",
            workflow,
        )
        self.assertIn("if: always()", workflow)
        self.assertIn("needs: [repository-tests, service-tests]", workflow)
        self.assertIn("REPOSITORY_TESTS_RESULT: ${{ needs.repository-tests.result }}", workflow)
        self.assertIn("SERVICE_TESTS_RESULT: ${{ needs.service-tests.result }}", workflow)
        self.assertIn("name: CI Gate", workflow)
        self.assertTrue(GATE.is_file(), "CI Gate needs a tested result evaluator")

    def test_gate_fails_when_any_test_job_failed_cancelled_or_skipped(self) -> None:
        cases = (
            ("failure", "success"),
            ("cancelled", "success"),
            ("success", "skipped"),
        )

        for repository_result, service_result in cases:
            with self.subTest(repository=repository_result, service=service_result):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(GATE),
                        "--repository-result",
                        repository_result,
                        "--service-result",
                        service_result,
                    ],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(0, result.returncode)

    def test_gate_passes_only_when_all_test_jobs_succeeded(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(GATE),
                "--repository-result",
                "success",
                "--service-result",
                "success",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
