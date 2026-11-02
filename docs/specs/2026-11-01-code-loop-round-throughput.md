# Code-loop round throughput — Spec

## Goal
Remove artificial one-task-per-round restrictions and default serialization across discover, handler and reviewer, while retaining item ownership and publication safety.

## Scope and behavior
- Ordinary handler and reviewer sessions repeatedly claim and complete available distinct items, with a separate execution, token, worktree, verification, report, publication and cleanup for each. Stop when no eligible item remains; avoid repeatedly claiming a failed or blocked item in the same session.
- Discover has one aggregate work item per discovery window. Permit publishing more than ten independently evidenced, deduplicated issues in a single discovery execution; preserve individual outbox identities, recovery and bounded payload validation.
- Allow different issues and PR repairs to run concurrently by default without the implicit whole-repository path lock. Honor explicitly configured issue scopes and retain issue, PR, branch, resource, lease and fencing protections. Independent branches may subsequently require conflict resolution.
- Remove the default parallel-execution ceiling; retain explicitly configured limits for compatibility. Remove cross-loop busy suppression in the event trigger and dispatch eligible matched work without losing unstarted matches, preserving safe concurrent trigger behavior and explicit rate settings.
- Keep dashboard targeted commands scoped to their requested item and keep single-claim API response compatibility.

## Non-goals and constraints
Do not weaken verification, reviewer independence, platform authorization, publication reconciliation or same-resource exclusion. Do not manufacture duplicate discovery scans, since simultaneous unpartitioned scans may create duplicate Issues. Preserve existing state and explicit configuration; no destructive migration. No automatic merge.

## Acceptance criteria
1. A normal handler or reviewer session can finish at least two distinct eligible items; empty or blocked queues terminate without retry loops.
2. Two independent issue/repair executions and multiple independent reviews can run concurrently by default; same issue, PR, branch or explicitly overlapping scope cannot be claimed concurrently. Explicit parallel limits still apply.
3. One discovery execution accepts more than ten valid findings; interrupted publication resumes only unresolved child operations and unknown receipts never trigger blind replay.
4. An unrelated running execution does not prevent event dispatch; multiple matches are not lost on partial spawn failure or concurrent invocations.
5. Targeted dashboard rounds stay targeted; existing API consumers and persisted data remain compatible. Tests cover contention, isolation, idempotency, configuration and end-to-end lifecycle.
