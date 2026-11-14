# Handler report identity

## Goal
Allow a Handler to truthfully report and publish ordinary PR delivery after completing its own work, while preserving independent PR review and the separate issue-closing safeguard.

## Scope and behavior
- For Handler reports for ordinary PR delivery, allow `reviewer` to equal the execution worker. The field remains required and represents the actual reporting identity; it is not proof of a separate review.
- On Handler `create_pr` / `edit_pr` publication, require a report for the same execution, current head and item version, plus passing current verification, authorization, and fencing. Do not require that report's identity differ from the worker.
- Preserve independent judgment for `close_issue`, Reviewer execution/report rules, developer self-review prohibition, coverage, required checks, and Discover behavior. Other Handler actions retain their existing checks unless essential for reporting non-publication outcomes.
- Update Handler documentation and the AgentDock scheduled task instructions after deployment so they accurately describe the contract. Do not change schedules or claim limits.

## Non-goals and compatibility
No new reporter role, identity spoofing, automatic merge, schema migration, or weakening of code review. Existing distinct-identity reports remain valid. The report interface remains unchanged. Identity strings are not authentication of an independent person.

## Acceptance criteria
1. A real Handler claim, bind, verify, same-worker report and PR publication succeeds with current evidence.
2. Missing/failed/stale verification or judgment, changed head or item version, invalid fencing, and unauthorized action remain rejected.
3. Reviewer identity/developer independence, review coverage and checks, Discover rules, and independent close-issue judgment remain enforced.
4. Focused tests demonstrate report and publish, full relevant suite passes in isolated state, independent review is complete, and the actual deployed service is verified before production claim of recovery.
5. Existing work and leases are preserved; no repeat PR publication or destructive cleanup is performed to simulate success.
