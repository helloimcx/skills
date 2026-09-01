# Project Setup Hard Static-Quality Gates Specification

## Goal

Make cyclomatic complexity, duplicate-code rate, dependency-cycle count, dead-code count, and long-function/large-file counts hard quality gates in `project-setup` initialization. Every initialized project with executable source must measure all five categories, run them through the aggregate quality command, and provide evidence that the configured thresholds pass.

## Scope

- Replace risk-based wording for the five requested metrics with mandatory gates.
- Define portable default thresholds while allowing a project to choose stricter thresholds.
- Require a discoverable command and canonical configuration or documentation for each gate.
- Keep Unit, Integration, E2E, Regression, Gherkin/BDD, format, lint, typecheck, build, security, and dependency checks unchanged except for their interaction with the aggregate gate.
- Add contract tests preventing the five metrics from reverting to optional-only wording.
- Synchronize project-setup references, repository architecture documentation, Spec, Plan, and an ADR.

## Non-goals

- Do not prescribe one analyzer or one programming language.
- Do not silently install quality tools or lower thresholds to make a project pass.
- Do not require these source-code metrics for repositories with no executable source; such a case must be evidenced as `[N/A]`.
- Do not grandfather existing violations as passing. A user-authorized migration baseline must remain visible as debt and must not permit new violations, but the ordinary initialization gate remains failed until the configured threshold is satisfied.

## Behavior and Interface

For projects with executable source, initialization must configure and run all five gates through the single `verify`/`qa`/`check` entrypoint:

| Gate | Default maximum | Passing rule |
|---|---:|---|
| Cyclomatic complexity | 10 per function/method | no measured function/method exceeds the limit |
| Duplicate-code rate | 5% of analyzed source | measured rate is at or below the limit |
| Dependency cycles | 0 | no cycle exists in the analyzed production dependency graph |
| Dead code | 0 reachable production dead-code findings | production dead-code finding count is 0 |
| Long functions | 80 logical source lines per function/method | no function/method exceeds the limit |
| Large files | 500 source lines per production file | no production file exceeds the limit |

Projects may select stricter thresholds and must record the metric definition, scope exclusions, tool/version, command, and threshold in their canonical quality configuration or `docs/quality-gates.md`. A tool's incompatible metric must be mapped explicitly; silently substituting a different measurement is not valid evidence.

The gate is hard: a non-zero command, threshold violation, missing analyzer, stale report, or unverifiable result is `[FAIL]` or `[BLOCKED]`, never `[PASS]`. `[N/A]` is allowed only when the project has no executable source, with evidence. Generated/vendor files may be excluded only through a committed, narrow, reviewable configuration. An existing migration baseline remains `[FAIL]` until the measured result reaches the threshold; authorization or a no-new-violations promise does not turn it into `[PASS]`.

## Acceptance Criteria

1. `verification.md` explicitly labels all five requested metric categories as mandatory hard gates and defines pass/fail/block semantics.
2. `production-baseline.md` requires the aggregate quality command to execute all five gates and documents portable default thresholds.
3. `project-setup/SKILL.md` tells the initializer to configure, execute, and report the five hard gates rather than merely evaluate them by risk.
4. Contract tests fail if any requested metric is described only as optional/risk-based or if hard-gate evidence semantics disappear.
5. Repository architecture documentation and an ADR describe the change without introducing a dependency on a particular analyzer.
6. Package tests, repository tests, the skill validator, Markdown-link checks, and diff checks pass.
