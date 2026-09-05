from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VIBE_CODING_DIR = REPO_ROOT / "vibe-coding"


class VibeCodingContractTests(unittest.TestCase):
    def test_vibe_coding_has_required_files(self) -> None:
        required = {
            VIBE_CODING_DIR / "SKILL.md",
            VIBE_CODING_DIR / "README.md",
            VIBE_CODING_DIR / "agents" / "openai.yaml",
            VIBE_CODING_DIR / "references" / "complexity.md",
            VIBE_CODING_DIR / "references" / "documentation-sync.md",
            VIBE_CODING_DIR / "references" / "parallel-planning.md",
            VIBE_CODING_DIR / "references" / "qa.md",
            VIBE_CODING_DIR / "references" / "review.md",
        }
        missing = [str(p.relative_to(REPO_ROOT)) for p in required if not p.is_file()]
        self.assertEqual([], missing, f"missing required files: {missing}")

    def test_scheme_diagram_contract_in_skill_and_references(self) -> None:
        skill = (VIBE_CODING_DIR / "SKILL.md").read_text(encoding="utf-8")
        planning = (
            VIBE_CODING_DIR / "references" / "parallel-planning.md"
        ).read_text(encoding="utf-8")

        for doc in (skill, planning):
            self.assertIn("archify compare", doc)
            self.assertIn("docs/architecture/changes/", doc)
            self.assertIn("fallback", doc.casefold())

        self.assertIn("docs/plans/", skill)
        self.assertIn("docs/specs/", skill)
        self.assertIn("Expected / Delta", skill)
        self.assertIn("Actual As-Built", skill)

    def test_worktree_timing_before_scheme_generation(self) -> None:
        skill = (VIBE_CODING_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("沙箱隔离先行", skill)
        self.assertIn("worktree", skill)

    def test_architecture_governance_and_lint_arch_integration(self) -> None:
        skill = (VIBE_CODING_DIR / "SKILL.md").read_text(encoding="utf-8")
        doc_sync = (
            VIBE_CODING_DIR / "references" / "documentation-sync.md"
        ).read_text(encoding="utf-8")
        review = (VIBE_CODING_DIR / "references" / "review.md").read_text(
            encoding="utf-8"
        )
        qa = (VIBE_CODING_DIR / "references" / "qa.md").read_text(encoding="utf-8")

        for doc in (skill, doc_sync, review, qa):
            self.assertIn("lint:arch", doc)

        for spec_type in (
            "system-architecture.json",
            "workflow.json",
            "sequence.json",
            "lifecycle.json",
            "overview.md",
        ):
            self.assertIn(spec_type, doc_sync)


if __name__ == "__main__":
    unittest.main()
