from __future__ import annotations

import re
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = SKILL_DIR.parent


class ProjectSetupSkillContractTests(unittest.TestCase):
    def test_skill_package_has_required_files(self) -> None:
        required = {
            SKILL_DIR / "SKILL.md",
            SKILL_DIR / "agents" / "openai.yaml",
            SKILL_DIR / "references" / "production-baseline.md",
            SKILL_DIR / "references" / "project-profiles.md",
            SKILL_DIR / "references" / "verification.md",
        }

        missing = sorted(
            str(path.relative_to(PACKAGE_PARENT)) for path in required if not path.is_file()
        )
        self.assertEqual([], missing, f"missing required skill files: {missing}")

    def test_frontmatter_name_and_description_define_the_routing_boundary(self) -> None:
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        match = re.match(r"\A---\n(?P<frontmatter>.*?)\n---\n", text, re.DOTALL)
        self.assertIsNotNone(match, "SKILL.md must start with YAML frontmatter")
        frontmatter = match.group("frontmatter")

        self.assertRegex(frontmatter, r"(?m)^name:\s*project-setup\s*$")
        description_match = re.search(r"(?m)^description:\s*(.+)$", frontmatter)
        self.assertIsNotNone(description_match, "frontmatter must contain a description")
        description = description_match.group(1).lower()
        self.assertIn("new", description)
        self.assertIn("project", description)
        self.assertTrue(
            any(term in description for term in ("established", "existing", "routine")),
            "description must exclude ordinary work in established projects",
        )

    def test_every_reference_is_linked_and_every_local_markdown_link_resolves(self) -> None:
        skill_file = SKILL_DIR / "SKILL.md"
        text = skill_file.read_text(encoding="utf-8")
        expected_references = {
            "references/production-baseline.md",
            "references/project-profiles.md",
            "references/verification.md",
        }
        links = set(
            target.split("#", 1)[0]
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text)
            if not target.startswith(("http://", "https://", "mailto:"))
        )

        self.assertTrue(expected_references.issubset(links))
        missing_targets = sorted(
            target for target in links if target and not (skill_file.parent / target).is_file()
        )
        self.assertEqual([], missing_targets, f"unresolved local links: {missing_targets}")

    def test_openai_metadata_explicitly_invokes_the_skill(self) -> None:
        metadata = (SKILL_DIR / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn('display_name: "Project Setup"', metadata)
        self.assertIn("$project-setup", metadata)

    def test_skill_has_no_runtime_dependency_on_delivery_workflow_skills(self) -> None:
        instruction_files = [SKILL_DIR / "SKILL.md", *(SKILL_DIR / "references").glob("*.md")]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in instruction_files)

        for forbidden in ("$vibe-coding", "worktree", "Approval Gate", "Subagent"):
            self.assertNotIn(
                forbidden.casefold(),
                combined.casefold(),
                f"project-setup must not depend on {forbidden}",
            )

    def test_skill_files_have_no_initializer_placeholders(self) -> None:
        placeholder_patterns = (
            "TODO: Complete",
            "TODO: Describe",
            "[TODO",
            "Your Skill Name",
            "Replace this",
        )
        checked_files = [SKILL_DIR / "SKILL.md", *(SKILL_DIR / "references").glob("*.md")]

        leftovers: list[str] = []
        for path in checked_files:
            text = path.read_text(encoding="utf-8")
            for pattern in placeholder_patterns:
                if pattern in text:
                    leftovers.append(f"{path.name}: {pattern}")

        self.assertEqual([], leftovers, f"unfinished initializer placeholders: {leftovers}")


if __name__ == "__main__":
    unittest.main()
