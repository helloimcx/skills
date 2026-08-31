# ADR 0002: Keep Architecture Facts Independent of the Diagram Provider

- Status: Accepted
- Date: 2026-08-31

## Context

Repositories initialized by `project-setup` need to keep their system architecture visible after later feature work and major refactors. Both Codex and Claude Code must recognize architecture-significant changes, preserve a meaningful history, and keep the root README's current architecture diagram synchronized.

Archify is a strong default renderer because it can validate typed architecture sources, deliver checked artifacts, and compare Architecture snapshots. However, making its JSON or generated HTML the only architecture record would couple repository semantics to one optional tool. Duplicating the complete maintenance procedure in both `AGENTS.md` and `CLAUDE.md` would also waste context and create drift.

## Decision

`project-setup` generates a durable, progressively disclosed architecture-governance contract:

- root `AGENTS.md` is the shared always-loaded source for a short semantic `Architecture Impact` trigger, an on-demand policy path, and a completion invariant;
- root `CLAUDE.md` preserves Claude-specific content and imports `@AGENTS.md` once; an existing `.claude/CLAUDE.md` without a root file is a preserving compatibility exception recorded in setup evidence;
- `docs/architecture/maintenance.md` owns the detailed later-maintenance procedure;
- `docs/architecture.md` owns provider-neutral current facts;
- `docs/architecture/changes/` owns append-only semantic history;
- `docs/architecture/diagram-provider.yaml` selects an active provider and fallback while preserving stable public output paths;
- provider sources and receipts stay under `docs/architecture/providers/<provider>/`;
- the root README contains exactly one managed current-diagram block.

Archify is the preferred provider when already available or explicitly authorized. It is not installed silently and is not a hard dependency. A declared fallback such as inline Mermaid keeps initialization truthful and usable when Archify is unavailable. Changing providers does not change the Agent trigger, current-facts format, history format, or stable README contract.

## Alternatives Considered

### Put the full workflow in AGENTS.md and CLAUDE.md

Rejected because the procedure is relevant only to architecture-significant work, consumes context in every task, and would drift across duplicate files.

### Store the full workflow only inside the project-setup skill

Rejected because later feature and refactor tasks do not necessarily invoke or retain `project-setup`; future Agents would have no reliable trigger or policy path.

### Make Archify mandatory and use its typed source as architecture truth

Rejected because provider availability, installation authorization, and future renderer choice are independent of the repository's architecture semantics.

### Rely only on Git history

Rejected because Git preserves bytes but does not provide a concise semantic account of changed responsibilities, boundaries, dependencies, data flow, and rationale.

## Consequences

- Codex and Claude Code share one short routing rule while loading detailed behavior only when required.
- Architecture facts and history remain readable and maintainable without Archify.
- Archify can contribute validation and Before/Delta/After evidence without owning semantic truth.
- Initialized repositories gain several small documentation artifacts and one managed README block.
- Prompt guidance improves consistency but does not become a security enforcement mechanism; repositories may add native documentation checks when stronger enforcement is warranted.
- Provider failures remain visible and preserve the last-known-good artifact instead of being reported as successful synchronization.
