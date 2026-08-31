# Project Setup Skill Specification

## Goal

Create a discoverable, independently installable `project-setup` skill that turns a new or agreed bootstrap-only directory into a minimal, production-oriented software project and stops after initialization has been verified.

## Scope

- Support greenfield applications, services, CLIs, libraries, workers, and justified monorepos.
- Inspect and classify the exact target before mutation.
- Resolve only initialization decisions: project shape, runtime, supported versions, package manager, public entrypoint, distribution/deployment shape, external boundaries, and material non-functional requirements.
- Prefer current official or ecosystem-standard generators when they fit; otherwise create the smallest manual scaffold.
- Establish one runnable public path and a production baseline for architecture, configuration, error handling, observability, security, tests, quality gates, reproducibility, and documentation.
- Maintain `docs/architecture.md`, `docs/specs/`, `docs/plans/`, and `docs/decisions/` with real bootstrap content.
- Validate the initialized project through its public entrypoint, complete applicable automated suite, aggregate quality command, and a clean-environment bootstrap.
- Update this repository's skill index and architecture documentation.

## Non-goals

- Do not implement product features beyond the minimal behavior needed to prove the scaffold.
- Do not act as a general feature-development, bug-fix, refactoring, planning, worktree, approval, or independent-review workflow.
- Do not load, invoke, require, or fall back to another workflow skill.
- Do not initialize ordinary feature work inside an established project.
- Do not provide a universal source-code template or prescribe one framework.
- Do not create remote repositories, cloud resources, production databases, external accounts, paid services, or global tools without explicit authorization.
- Do not install this skill into an agent's personal skill directory as part of this task.
- Do not modify the existing `vibe-coding` package.

## Behavior and Interface

### Invocation boundary

Activate for requests to create, scaffold, bootstrap, or initialize a new software project or an explicitly agreed bootstrap-only directory. Do not activate for routine features, bug fixes, or refactors in an established codebase.

### Independent operation

The skill contains its complete initialization workflow and references only resources inside its own package. It does not inspect whether another coding skill is installed, transfer control to one, or inherit another skill's planning and review lifecycle. After reporting initialization evidence, it stops. Any later feature request is a separate task.

### Safe preflight

- Resolve and disclose the exact absolute target before mutation.
- Classify it as absent, empty, bootstrap-only, or established.
- Preserve existing files by default and never silently force, clean, or overwrite a non-empty target.
- Ask only questions that materially change project shape, compatibility, data ownership, security, deployment, licensing, or external permissions.
- When the user has specified the stack and target is empty, choose ordinary implementation details autonomously.
- Verify temporally unstable versions and generator commands against current primary documentation when needed.

### Initialized baseline

The project must contain the smallest architecture that supports its declared public entrypoint, plus:

- explicit module responsibilities, interfaces, and one-way dependencies;
- centralized validated configuration and a safe `.env.example` when environment variables exist;
- stable external error behavior and preserved internal error context where applicable;
- structured, secret-safe diagnostics and request/job/command correlation where applicable;
- input validation and a documented security trust boundary;
- ecosystem-standard, tool-generated lock files when they carry real dependency-resolution or build meaning; otherwise an explicit not-applicable decision rather than a handmade empty lock;
- meaningful Unit, Integration, E2E, and Regression conventions, with only applicable layers implemented and non-applicable layers explained;
- locally runnable format, lint, type/static analysis, test, build/package, security/dependency, and aggregate verification commands supported by the selected ecosystem;
- CI parity when a CI platform is known, without silently creating or binding a remote provider;
- truthful setup, run, test, build, and verification documentation;
- real bootstrap architecture, Spec, Plan, and necessary ADR content.

### Testing behavior

- Use `RED -> GREEN -> REFACTOR` for custom bootstrap behavior.
- Validate generated configuration and docs through parsers, structural checks, commands, or links rather than fake code-level RED steps.
- Define E2E through the project type's public surface: process invocation for CLI, consumer installation for library, public protocol for service, browser path for UI, or real consumption entry for worker.
- Do not invent Regression cases before a real defect exists; provide a discoverable entry and require future fixes to add a reproducer first.
- Do not use ignored, skipped, weakened, or content-free tests to manufacture green gates.

### Completion boundary

Run all applicable initialization checks, verify a success and critical failure path, perform a clean-environment bootstrap, synchronize docs, report evidence, and stop. Do not continue into general product development in the same initialization task.

## Constraints and Compatibility

- Preserve user-provided technology, package-manager, licensing, repository, and deployment choices.
- Prefer the simplest design that satisfies known requirements; avoid speculative infrastructure and abstraction.
- Treat unavailable tools or inapplicable checks as `[BLOCKED]` or `[N/A]` with reasons, never `[PASS]`.
- Core initialization failures must be explicit. Optional conveniences may degrade only when the limitation is visible.
- Preserve original errors, stacks/context, and correlation identifiers internally; expose stable, non-sensitive errors at public boundaries.
- Never commit or log real secrets.

## Acceptance Criteria

1. `project-setup/SKILL.md` has valid frontmatter, a discriminating trigger description, exact-target protection, an initialization-only workflow, and an explicit stop boundary.
2. The distributable skill and its references contain no runtime dependency on another workflow skill and do not require worktrees, approval gates, or independent agents.
3. `agents/openai.yaml` presents consistent UI metadata and explicitly invokes `$project-setup` in its default prompt.
4. All local Markdown references resolve, contain no initializer placeholders, and progressively separate production baseline, project profiles, and initialization verification.
5. The skill encodes the requested architecture, Docs as Code, TDD, quality gates, explicit errors, observability, root-cause fixes, failure-aware external/data design, security defaults, complexity control, small changes, and completion checks without forcing irrelevant infrastructure.
6. Lock-file guidance follows ecosystem semantics and forbids handmade comment-only or placeholder locks.
7. The repository README and `CLAUDE.md` list `project-setup` without describing a runtime dependency on another skill.
8. Package and repository contract tests pass both in the source checkout and after copying the distributable skill to an isolated directory; the bundled skill validator accepts it.
9. An independent forward test in a fresh temporary directory demonstrates the final skill can initialize and clean-room verify a realistic project without unauthorized writes, external actions, empty locks, or workflow-skill coupling.
10. An independent review reports no unresolved blocking finding, and the final diff contains no task-authored change under `vibe-coding/`.
