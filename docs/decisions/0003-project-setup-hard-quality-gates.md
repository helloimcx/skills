# ADR 0003: Make Core Static-Quality Metrics Hard Initialization Gates

- Status: Accepted
- Date: 2026-09-01

## Context

`project-setup` already asks projects to assess complexity, duplication, dependency cycles, dead code, and oversized functions/files, but the wording permits risk-based omission. That makes the quality bar inconsistent and allows a project to claim verification without measuring basic maintainability risks.

## Decision

For any initialized project with executable source, the aggregate `verify`/`qa`/`check` command must execute five hard source-quality gate categories:

- cyclomatic complexity: maximum 10 per function/method;
- duplicate-code rate: maximum 5% of analyzed source;
- dependency cycles: zero;
- reachable production dead code: zero;
- long functions: maximum 80 logical source lines, and large production files: maximum 500 source lines.

Projects may choose stricter limits. Each project records metric definitions, scope, exclusions, analyzer/version, command, and threshold in canonical quality configuration or `docs/quality-gates.md`. A missing analyzer, failed command, violation, stale/unverifiable report, or unauthorized exclusion is not a pass. No executable source permits `[N/A]` only with evidence; tool absence is `[BLOCKED]`, not `[N/A]`.

No particular analyzer is part of the skill contract. The initializer selects a maintained stack-appropriate tool and must not silently install a global dependency.

## Consequences

- New projects receive a consistent, measurable maintainability floor.
- The aggregate quality command becomes the single evidence-producing interface for these metrics.
- Existing or generated-code-heavy repositories may need narrow, committed exclusions or a user-authorized migration plan; those exceptions remain visible and cannot hide new violations.
- Tool choice remains portable, but each project must make its metric semantics and thresholds explicit.
