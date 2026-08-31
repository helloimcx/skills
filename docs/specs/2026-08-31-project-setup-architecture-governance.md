# Project Setup Architecture Governance Specification

## Goal

Extend `project-setup` so every initialized repository receives a durable, progressively disclosed architecture-maintenance contract. Codex and Claude Code must recognize when a later code change affects architecture, load the detailed policy on demand, keep current architecture facts and explicit change history synchronized, and keep the README's architecture diagram current. Archify is the preferred diagram provider, not the source of architectural truth or a hard runtime dependency.

## Scope

- Keep a short architecture-impact trigger and routing rule in the generated root `AGENTS.md`.
- Make generated root `CLAUDE.md` import the shared `AGENTS.md` rules with `@AGENTS.md`, while preserving existing Claude-specific instructions. If an existing project only has `.claude/CLAUDE.md`, preserve it as a compatibility exception and record the actual location.
- Generate a detailed, agent-neutral `docs/architecture/maintenance.md` policy from a reusable skill asset.
- Generate a provider manifest that separates stable architecture outputs from provider-specific source and receipts.
- Prefer Archify when it is already available or the user authorizes installation; otherwise use the declared fallback without blocking unrelated project initialization.
- Define semantic architecture-impact criteria based on responsibilities, boundaries, dependency direction, protocols, data ownership/flow, deployment/trust boundaries, and external integrations rather than changed-line or file counts.
- Require architecture-significant changes to synchronize current architecture facts, one append-only change record, provider artifacts, validation evidence, and the managed README architecture block.
- Preserve provider-neutral current facts in `docs/architecture.md` and provider-neutral history in `docs/architecture/changes/`.
- Update the distributable skill's contract tests and this repository's architecture and decision documentation.

## Non-goals

- Do not run `project-setup` during every later feature, bug fix, or refactor.
- Do not make Archify JSON, HTML, or SVG the sole record of system architecture.
- Do not install Archify or another global tool without explicit authorization.
- Do not require diagram churn for internal refactors that preserve architectural responsibilities, boundaries, dependencies, protocols, and data flow.
- Do not turn natural-language Agent instructions into a substitute for enforceable permissions or security controls.
- Do not modify or couple `project-setup` to a general feature-delivery workflow skill.

## Behavior and Interface

### Progressive disclosure

`project-setup/SKILL.md` remains a concise initialization router. It directs the initializing Agent to read `references/architecture-governance.md` when creating architecture documents and persistent Agent guidance. That reference defines safe merge behavior and the generated repository contract. `assets/architecture-maintenance.md` is adapted into the initialized repository as `docs/architecture/maintenance.md`.

The generated repository keeps only the trigger, routing link, and completion invariant in `AGENTS.md`. Detailed provider, history, README, validation, fallback, and failure procedures live in `docs/architecture/maintenance.md` and load only after the trigger fires.

### Cross-Agent routing

- Codex receives the shared rule from the root `AGENTS.md`.
- Claude Code receives the same rule through an idempotent `@AGENTS.md` import in the root `CLAUDE.md`.
- Existing instruction files and unrelated content are preserved.
- Managed markers make repeated initialization or incremental baseline completion idempotent and prevent duplicate blocks.

### Architecture impact

Before implementing a change, the future coding Agent classifies `Architecture Impact` as `Required` or `None`. `Required` applies when the change alters one or more of:

- a runtime component, service, or major module responsibility;
- dependency direction or a public protocol;
- data ownership, storage, or primary data flow;
- a trust, deployment, process, or network boundary;
- an external integration or synchronous/asynchronous communication mode;
- addition, removal, split, or merge of an architectural component.

When impact is `Required`, the Agent reads `docs/architecture/maintenance.md` before implementation and does not report completion until current architecture facts, one change record, current provider artifacts, validation evidence, and the README diagram agree. Internal refactors that preserve these semantics use `None` and do not rewrite the diagram.

### Provider contract

The initialized repository records a provider manifest under `docs/architecture/diagram-provider.yaml`. It identifies:

- the active provider and optional fallback;
- provider capabilities for validation, README rendering, and optional comparison;
- stable public output paths for the current README image, optional interactive artifact, and history directory;
- provider-specific source and receipt paths.

Stable public output paths do not change when the provider changes. Provider-specific sources remain isolated under `docs/architecture/providers/<provider>/`.

Archify is preferred when available because it can validate typed architecture input, atomically deliver an artifact, and compare Architecture snapshots. Its installation, version, and command location must be discovered at execution time. Absence of Archify must not cause silent installation or a false pass. The Agent either uses the declared fallback, obtains authorization for installation, or reports the diagram step as blocked while preserving the last verified output.

### Current architecture and history

- `docs/architecture.md` is the provider-neutral statement of current architecture facts and code evidence.
- `docs/architecture/changes/YYYY-MM-DD-<slug>.md` records the semantic delta, rationale or ADR link, affected boundaries, and evidence for each architecture-significant change.
- Git retains prior provider sources; providers with comparison support may also emit a checked delta artifact and receipt for a change.
- History entries never claim inferred risk, runtime impact, or merge safety without evidence.

### README contract

The README contains exactly one managed architecture block bounded by stable markers. It displays the current stable SVG/PNG output, or an inline Mermaid fallback when no static provider artifact exists, and links to current facts and history. Initialization and later architecture maintenance preserve unrelated README content and never leave a broken local artifact link.

## Constraints and Compatibility

- Preserve existing `AGENTS.md`, `CLAUDE.md`, README, architecture documents, and repository-specific rules.
- Use relative, repository-local paths in generated guidance and README links.
- Keep the always-loaded routing block short and specific enough to decide when the detailed policy must be read.
- Do not rely on Claude-only `.claude/rules/` for shared behavior.
- Do not rely on the `project-setup` skill still being invoked or installed during later feature work.
- Keep Archify-specific source, receipts, commands, and format details outside provider-neutral architecture facts and change history.
- Treat missing providers, failed validation, stale artifacts, and unresolved README links as visible `[BLOCKED]` or `[FAIL]` evidence, never `[PASS]`.

## Acceptance Criteria

1. `project-setup/SKILL.md` routes architecture-governance setup to a linked reference without embedding the full later-maintenance procedure.
2. The distributable skill contains `references/architecture-governance.md` and `assets/architecture-maintenance.md`, and all required local links resolve.
3. The reference requires an idempotent root `AGENTS.md` trigger/router and a preserving `@AGENTS.md` import in root `CLAUDE.md`.
4. The always-loaded rule defines semantic architecture-impact triggers, requires on-demand reading of `docs/architecture/maintenance.md`, and retains the synchronization completion invariant.
5. The generated-policy asset defines provider-neutral current facts, append-only semantic history, manifest-declared stable SVG/PNG or inline-Mermaid README output, provider isolation, validation/failure behavior, and README markers.
6. Archify is described as a preferred replaceable provider; no skill instruction silently installs it or makes project initialization depend on its availability.
7. The provider contract supports a fallback and allows provider replacement without changing `AGENTS.md`, history format, current-facts format, or the README's stable artifact path.
8. Package tests fail when the architecture-governance reference, generated-policy asset, routing link, cross-Agent import behavior, provider boundary, or README synchronization contract is missing.
9. Repository architecture documentation and an ADR describe the new persistent-guidance and replaceable-provider boundary.
10. Package tests, repository tests, the bundled skill validator, Markdown link checks, and diff checks pass after implementation.
