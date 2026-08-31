# ADR 0001: Keep Project Setup Independent and Initialization-Only

- Status: Accepted
- Date: 2026-08-31

## Context

`project-setup` initializes production-oriented projects across services, Web applications, CLIs, libraries, and workers. This repository also contains general coding workflows, but repository co-location does not imply that an initialization skill should inherit or invoke them.

An earlier design treated a general coding workflow as an optional outer lifecycle. In practice, that pulled complexity classification, approval, isolated-branch, independent-review, and full feature-delivery concepts into a skill whose sole user-facing job is to create and verify a new project. The result obscured the stopping condition and created two responsibilities in one package.

## Decision

`project-setup` is independently installable and owns only initialization concerns:

- exact target resolution and overwrite protection;
- project-shape and toolchain decisions;
- official generator selection;
- minimal runnable public path;
- production baseline for architecture, Docs as Code, configuration, errors, observability, security, tests, quality commands, and reproducibility;
- public-path and clean-environment initialization verification;
- an evidence report followed by an explicit stop.

The package does not discover, load, invoke, require, or provide a fallback for another workflow skill. It does not continue into general product development. Detailed baseline, project-profile, and verification guidance remains progressively disclosed inside the package.

## Alternatives Considered

### Use a general coding workflow as an outer lifecycle

Rejected because it couples initialization to planning and review behavior that is not part of the user's setup request, makes the skill harder to install independently, and blurs the completion boundary.

### Copy a complete development lifecycle into Project Setup

Rejected because it duplicates another responsibility and would drift over time.

### Ship framework-specific starter templates

Rejected because versions and best practices change independently, templates imply unsupported product decisions, and a universal starter cannot fit all supported project profiles.

## Consequences

- The skill starts and ends with project initialization.
- It remains usable when installed alone.
- Empty, clearly specified targets can be initialized autonomously without an unrelated planning gate.
- Destructive overwrite and external actions still require explicit authorization because they are initialization safety boundaries, not development-process coupling.
- Later feature development is a separate user request handled by whatever development workflow the user chooses.
- Forward tests must evaluate the skill in isolation and must not assume another skill is available.
