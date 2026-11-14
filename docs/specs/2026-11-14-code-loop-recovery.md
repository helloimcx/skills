# Code-loop recovery

## Goal
Restore reliable project execution and fresh dashboard snapshots after the observed scheduler and collection outage.

## Scope and behavior
- A running control-plane server periodically collects all registered dashboard projects and evaluates alerts without a browser or automation round.
- Collection is single-flight with manual collection, isolates failures, and stops with the server. A configurable cadence permits disabling it.
- Diagnose and correct the supported automation network permission configuration for both registered projects, without editing application databases or weakening validation rules. Verify actual permissions in a new execution before claiming recovery.
- Preserve existing worktrees, branches, credentials and pending executions; do not overwrite PR branches or assume an expired lease can be removed manually.

## Constraints and acceptance
- Existing server, UI and operator routes remain compatible; no new listener or unauthenticated network surface.
- Periodic and manual collection cannot overlap; a failure must be logged and the next interval must run.
- Tests demonstrate startup, cadence, error recovery, exclusion, and shutdown. A real server check must show successive successful snapshots without page interaction.
- A newly started automation can query its registered control plane and makes a verifiable state transition; report any unsupported permission setting or external scheduler limitation rather than claiming success.
- Preserve all pre-existing work and record actual deployments and rollback steps.
