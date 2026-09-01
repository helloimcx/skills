from __future__ import annotations

import re
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = SKILL_DIR.parent


def instruction_files() -> list[Path]:
    return [
        SKILL_DIR / "SKILL.md",
        *(SKILL_DIR / "references").glob("*.md"),
        *(SKILL_DIR / "assets").glob("*.md"),
    ]


def code_blocks(text: str) -> list[str]:
    return re.findall(r"```[^\n]*\n(.*?)```", text, re.DOTALL)


class ProjectSetupSkillContractTests(unittest.TestCase):
    def test_skill_package_has_required_files(self) -> None:
        required = {
            SKILL_DIR / "SKILL.md",
            SKILL_DIR / "agents" / "openai.yaml",
            SKILL_DIR / "references" / "production-baseline.md",
            SKILL_DIR / "references" / "project-profiles.md",
            SKILL_DIR / "references" / "verification.md",
            SKILL_DIR / "references" / "architecture-governance.md",
            SKILL_DIR / "assets" / "architecture-maintenance.md",
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
            "references/architecture-governance.md",
        }
        links = {
            target.split("#", 1)[0]
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text)
            if not target.startswith(("http://", "https://", "mailto:"))
        }

        self.assertTrue(expected_references.issubset(links))
        missing_targets: list[str] = []
        for source in instruction_files():
            source_text = source.read_text(encoding="utf-8")
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", source_text):
                target = target.split("#", 1)[0]
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                if target and not (source.parent / target).is_file():
                    missing_targets.append(
                        f"{source.relative_to(SKILL_DIR)} -> {target}"
                    )
        self.assertEqual([], missing_targets, f"unresolved local links: {missing_targets}")

    def test_architecture_governance_is_progressively_disclosed(self) -> None:
        skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        governance = (
            SKILL_DIR / "references" / "architecture-governance.md"
        ).read_text(encoding="utf-8")

        self.assertIn("references/architecture-governance.md", skill)
        self.assertIn("../assets/architecture-maintenance.md", governance)
        self.assertNotIn("project-setup:architecture-maintenance:start", skill)

    def test_generated_agent_guidance_has_one_shared_cross_agent_router(self) -> None:
        governance = (
            SKILL_DIR / "references" / "architecture-governance.md"
        ).read_text(encoding="utf-8")
        router = next(
            block
            for block in code_blocks(governance)
            if "project-setup:architecture-maintenance:start" in block
        )

        for invariant in (
            "AGENTS.md",
            "CLAUDE.md",
            "@AGENTS.md",
            "project-setup:architecture-maintenance:start",
            "docs/architecture/maintenance.md",
            "Architecture Impact",
        ):
            self.assertIn(invariant, governance)

        self.assertEqual(
            1, router.count("project-setup:architecture-maintenance:start")
        )
        self.assertEqual(1, router.count("project-setup:architecture-maintenance:end"))
        for semantic_trigger in (
            "process",
            "network",
            "synchronous/asynchronous",
            "adds, removes, splits, or merges",
        ):
            self.assertIn(semantic_trigger, router.casefold())

        self.assertIn("preserve", governance.casefold())
        self.assertIn("idempotent", governance.casefold())

        claude_import = next(
            block
            for block in code_blocks(governance)
            if "project-setup:shared-agent-rules:start" in block
        )
        self.assertEqual(1, claude_import.count("project-setup:shared-agent-rules:start"))
        self.assertEqual(1, claude_import.count("project-setup:shared-agent-rules:end"))
        self.assertEqual(1, claude_import.count("@AGENTS.md"))

    def test_provider_manifest_examples_cover_archify_and_fallback_shapes(self) -> None:
        governance = (
            SKILL_DIR / "references" / "architecture-governance.md"
        ).read_text(encoding="utf-8")
        manifests = [
            block
            for block in code_blocks(governance)
            if block.lstrip().startswith("schema_version: 1")
        ]
        self.assertEqual(2, len(manifests))

        archify = next(block for block in manifests if "active: archify" in block)
        self.assertIn(
            "source: docs/architecture/providers/archify/system.architecture.json",
            archify,
        )
        self.assertIn(
            "receipt: docs/architecture/providers/archify/system.receipt.json",
            archify,
        )
        self.assertIn("mode: static-image", archify)

        fallback = next(block for block in manifests if "active: mermaid" in block)
        self.assertIn(
            "source: docs/architecture/providers/mermaid/system.mermaid", fallback
        )
        self.assertIn("receipt: null", fallback)
        self.assertIn("outputs:\n  readme: docs/architecture/system-architecture.svg", fallback)
        self.assertIn("mode: inline-mermaid", fallback)

    def test_readme_policy_template_has_unique_managed_markers(self) -> None:
        policy = (
            SKILL_DIR / "assets" / "architecture-maintenance.md"
        ).read_text(encoding="utf-8")
        readme_block = next(
            block
            for block in code_blocks(policy)
            if "project-setup:architecture-diagram:start" in block
        )
        self.assertEqual(1, readme_block.count("project-setup:architecture-diagram:start"))
        self.assertEqual(1, readme_block.count("project-setup:architecture-diagram:end"))
        self.assertIn("outputs.readme", policy)

    def test_generated_policy_keeps_facts_history_readme_and_provider_in_sync(self) -> None:
        policy = (
            SKILL_DIR / "assets" / "architecture-maintenance.md"
        ).read_text(encoding="utf-8")
        folded = policy.casefold()

        for invariant in (
            "architecture impact",
            "docs/architecture.md",
            "docs/architecture/changes/",
            "docs/architecture/diagram-provider.yaml",
            "project-setup:architecture-diagram:start",
            "outputs.readme",
            "svg",
            "validation",
            "fallback",
        ):
            self.assertIn(invariant, folded)

        for semantic_trigger in (
            "responsibilit",
            "dependency direction",
            "data ownership",
            "trust",
            "deployment",
            "external integration",
        ):
            self.assertIn(semantic_trigger, folded)

    def test_archify_is_a_replaceable_provider_not_a_hard_dependency(self) -> None:
        governance = (
            SKILL_DIR / "references" / "architecture-governance.md"
        ).read_text(encoding="utf-8")
        policy = (
            SKILL_DIR / "assets" / "architecture-maintenance.md"
        ).read_text(encoding="utf-8")
        combined = f"{governance}\n{policy}"
        folded = combined.casefold()

        for invariant in (
            "archify",
            "github.com/tt-a1i/archify",
            "replaceable",
            "fallback",
            "providers/<provider>",
            "[blocked]",
        ):
            self.assertIn(invariant, folded)

        for forbidden in ("npx skills add", "must install archify"):
            self.assertNotIn(forbidden, folded)

    def test_openai_metadata_explicitly_invokes_the_skill(self) -> None:
        metadata = (SKILL_DIR / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn('display_name: "Project Setup"', metadata)
        self.assertIn("$project-setup", metadata)

    def test_skill_has_no_runtime_dependency_on_delivery_workflow_skills(self) -> None:
        combined = "\n".join(
            path.read_text(encoding="utf-8") for path in instruction_files()
        )

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
        checked_files = instruction_files()

        leftovers: list[str] = []
        for path in checked_files:
            text = path.read_text(encoding="utf-8")
            for pattern in placeholder_patterns:
                if pattern in text:
                    leftovers.append(f"{path.name}: {pattern}")

        self.assertEqual([], leftovers, f"unfinished initializer placeholders: {leftovers}")

    def test_requested_static_quality_metrics_are_hard_gates(self) -> None:
        verification = (SKILL_DIR / "references" / "verification.md").read_text(
            encoding="utf-8"
        )
        baseline = (SKILL_DIR / "references" / "production-baseline.md").read_text(
            encoding="utf-8"
        )
        entrypoint = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        combined = f"{verification}\n{baseline}\n{entrypoint}"
        hard_gate_section = verification.split("### 3.1", 1)[1].split(
            "其他质量项", 1
        )[0]

        for metric in (
            "圈复杂度",
            "重复代码率",
            "循环依赖数量",
            "Dead Code",
            "超长函数",
            "超大文件",
        ):
            self.assertIn(metric, combined)
            self.assertIn(metric, hard_gate_section)

        for invariant in (
            "硬门禁",
            "verify",
            "qa",
            "check",
            "5%",
            "80",
            "500",
            "[BLOCKED]",
            "[N/A]",
            "[FAIL]",
        ):
            self.assertIn(invariant, hard_gate_section + baseline + entrypoint)

        self.assertIn("必须由单一", hard_gate_section)
        self.assertIn("所有可观测值都必须测量、执行并由聚合命令报告", hard_gate_section)

        for threshold in (
            "≤ 10",
            "≤ 5%",
            "| 循环依赖数量 | 0 |",
            "| Dead Code | 0 |",
            "≤ 80",
            "≤ 500",
        ):
            self.assertIn(threshold, hard_gate_section)

        self.assertNotIn("持续评估但不机械安装所有工具", verification)
        self.assertNotIn("按项目风险接入覆盖率、复杂度、重复、循环依赖、Dead Code", baseline)


if __name__ == "__main__":
    unittest.main()
