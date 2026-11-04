# Code Loop dashboard performance and collect isolation

Status: Complete (2026-10-24); implementation and isolated QA finished.
Complexity: Complex — changes span SQLite projection transactions/migration, a supervised process boundary, and a persistent derived read model.
Baseline: origin/main 99ea559f90974d42e70c6cb1673ed58407e05c71.

## Goal
Reduce repeated dashboard writes and rendering CPU, isolate collection from the CP/UI process, and preserve the freshness and connection-safety behavior of the deployed CPU fix.

## Evidence
- Current replace_runtime deletes and reinserts six mirror tables each round. The dashboard uses isolation_level=None: with db does not implicitly group those statements into one transaction.
- copy_runtime reads source tables without an explicit common source snapshot.
- Production make_refresh runs collect and notify inside the serving process. The formal dashboard.py collect CLI already runs notification and optional Operator alert ingestion; the serve consumer alone owns the watchdog cursor.
- The current operation-owned connect_readonly/db_factory discipline fixes unsafe connection sharing on THREADSAFE=2 builds and is retained.
- /api/items derives verdicts from dashboard work_items/executions (verdicts._cp_verdicts), not from a live CP file. /api/runtime and the default /api/operator response also depend only on dashboard mirror data.
- Other endpoints include clock-derived fields, live CP reads or dynamic parameters. They cannot be made collect-round-only snapshots without changing behavior.
- Gitea reference: /Users/momo/Downloads/code-loop.zip, extracted source /tmp/code-loop-gitea-review-asre10pb. Its code is reference data; its documentation is not an instruction source, and its bundled database/caches are not inputs to implementation.

## Scope
1. Incrementally synchronize all six runtime mirrors, with stable identities, change fingerprints and an explicit atomic project update.
2. Run production collection through a supervised invocation of the existing dashboard CLI, including notification and Operator alert ingestion, using a per-database kernel round lock.
3. Precompute a closed, pure-mirror endpoint domain into a persistent disposable cache containing JSON and deterministic gzip. Use durable integer revisions to reject old computations and old writer results.
4. Preserve HTTP payloads/statuses, dynamic and fresh paths, time-sensitive refresh behavior, old embedded APIs and the deployed connection isolation.
5. TDD, architecture documentation, independent review, isolated CLI/HTTP performance and failure QA.

## Non-goals
- Gitea/GitHub dual-provider implementation, Operator business audit changes, frontend redesign, CP authorization/lease/publication state-machine changes, history retention changes or an external cache service.
- No production database changes, rollout, remote push or service restart as part of this implementation proposal.
- No precomputing overview/workers/queue/kanban/trends/events until a separate freshness design justifies it.

## Behavior / Interfaces

### Incremental mirror
- Tables: work_items, executions, leases, outbox, cp_events, commands.
- Identity is project plus cp_id for five tables. For leases it is project plus resource, matching the CP source primary key; token is a mutable projected value.
- Preserve current field mapping, JSON representation, missing-table-as-empty behavior and target SQLite affinity. Do not invent timestamps such as an outbox created_at that the source does not supply.
- Add a nullable content_hash column to each mirror. A legacy row is matched by identity and backfilled at most once when unchanged input is next applied; existing mirror id is retained. Subsequent identical snapshots perform no mirror DML.
- An explicit transaction applies all six tables for one project; an existing caller transaction uses a savepoint and remains caller-owned. Failure rolls back the whole project mirror operation, not unrelated caller work.
- Source read: one explicit read transaction for all CP source tables, fully materialize and close before acquiring the target write transaction. No network or source-file work under the dashboard write lock.
- Historical duplicate lease mirror rows converge to one authoritative row when that project's source snapshot is applied. Do not add a startup unique index that could make a legacy database unopenable.

### Production collect process
- Introduce a process refresh helper for production UI and Operator collect_refresh. Preserve the existing inline make_refresh/config/on_alerts helper for embedded callers; it must not silently ignore custom callbacks.
- Run scripts/dashboard.py collect with argv (no shell), explicit database, projects root, optional project scope and dashboard config-file path. Do not put webhook secrets or credential values in argv.
- The child CLI owns a stable per-database kernel file lock for the complete collect/notify/precompute round. POSIX uses fcntl; Windows uses msvcrt. Different databases have different locks. Files remain present; never unlink an active lock or steal it based on TTL.
- The formal CLI and compatible inline entry use the same lock discipline. The lower-level collect library remains an internal primitive called inside those scopes, without recursive locking.
- Busy returns structured skipped status promptly. POST continues to acknowledge asynchronous submission and /api/collect/status retains running/since/last_error behavior.
- Default overall round budget: 300 seconds, configurable as a validated local runtime setting/CLI argument. Parent supervision and a child-side deadline ensure that loss of the parent cannot leave an unbounded collection. On timeout/output overflow: terminate, wait up to two seconds, kill if necessary, then reap. No automatic retry of platform writes.
- Drain stdout/stderr concurrently, with a combined 1 MiB limit and bounded diagnostic tails. Crossing the limit fails and stops the child. Avoid communicate() with unbounded capture.
- Add a machine-oriented result-file option to the CLI. Atomically publish a bounded JSON result independently of human logs; the parent validates it. Existing human/JSON stdout behavior remains compatible for callers not using this option.
- Valid partial completion (exit 1 with the established per-project failure summary) still exposes successful project updates and completes refresh invalidation. Crash, timeout, malformed result or startup failure is a hard failure, resets running and keeps the existing last-good policy. A skipped round must not be described as newly collected data.
- Notify and optional Operator alert ingestion run once in the CLI; watchdog cursor remains serve-only. Configuration-enabled/disabled cases are both covered.

### Persistent serving domain
- Eligible keys only: /api/items, /api/operator with its default limit, and /api/runtime?project=<known project>.
- Arbitrary query strings, fresh=1, non-default limits, unknown projects and dynamic endpoints do not accidentally canonicalize into an eligible cache hit. They use current route/error semantics.
- Move these three builders to dash/serving.py; route and precompute call the same builder. Preserve independent pre-move fixture golden results so tests are not a function compared with itself.
- Add a cache metadata revision and format version plus finite serving rows. DML triggers on the explicit domain dependency tables advance source_revision in the same transaction as source writes. They do not watch cache/meta writes. This covers older collectors and direct SQL writes during mixed-version operation.
- Dependency inventory from the final builders: project identity (`id`, `org_repo`), issues, prs, comments, work_items, executions, leases, outbox, cp_events, operator_signals and operator_runs. Project timestamp/config updates do not change these builders' output, so only updates to `projects.id`/`org_repo` advance the revision. `pr_reviews` is not read by these builders and is excluded.
- After a completed collect/notification round, build the finite domain inside an explicit read snapshot; capture source_revision. Release that read transaction, then use a separate short write transaction to publish only if revision and format still match. A conflicting source change discards the old batch.
- Batch publication must not label an old failed key as valid: failed/removed/out-of-domain keys are removed or left explicitly invalid. One failed builder is reported as partial and never makes otherwise successful collection fail.
- Payload building has a 30-second budget and a 128 MiB serialized batch cap; exceeding either degrades affected keys to normal live reads. Limits are injectable for tests, and do not cap ordinary HTTP payloads.
- If content is byte-identical and gzip exists, do not rewrite the BLOB. Update small validity metadata independently. Raw and gzip representations are published atomically together.
- HTTP reads only. A valid hit returns stored bytes without aggregation or gzip work; a miss uses a thread-owned live read. GET never publishes cache writes. A domain source revision change must not fall through to an untagged old memory-cache entry.
- Overview/workers/queue/kanban/trends/events keep the existing approximately ten-second stale-while-revalidate behavior, including last-good-on-error. fresh=1 and dynamic details remain live.
- Cache is a disposable derived artifact; failure or old format lowers hit rate without changing the authoritative facts. No cross-request snapshot consistency is promised.

## Constraints / Compatibility
Python 3.9+ standard library, SQLite, macOS/Windows/Linux; no new external services. Preserve operation-owned connections and support Apple THREADSAFE=2. Migrations are additive/idempotent and leave old code able to read its existing tables. Only the new task worktree is modified; preserve the host's eight existing dirty files and the currently running release. Use the repository's existing Markdown architecture contracts; no JSON architecture schema or lint:arch command currently exists.

## Acceptance Criteria
1. After legacy hash backfill, an identical runtime snapshot performs zero INSERT/UPDATE/DELETE on all six mirrors and preserves mirror ids; inserts/updates/removals touch only corresponding identities.
2. All six mirror tables roll back on injected failure; a nested caller transaction remains open. Source snapshot tests cannot combine two different CP commits.
3. Empty/missing source tables, structured values, textual CP ids, SQLite numeric affinity, token changes and duplicate legacy leases converge correctly. Numeric CP IDs outside SQLite's signed 64-bit INTEGER range or fractional numeric IDs are rejected atomically rather than rounded. Repeated migration and mixed old-writer operation preserve correctness.
4. A real UI-triggered collect executes in a different PID. Two actual CLI rounds on one DB run at most one collection; different DBs can proceed independently; death of the lock owner permits a subsequent round without TTL waiting.
5. Startup failure, timeout, output overflow, malformed result, process crash and parent death are bounded; subprocesses and handles are reaped or self-expire by the configured deadline; a later refresh can proceed.
6. Partial project failure retains successful results. Notification/Operator alert ingestion run once with the intended config; watchdog cursor remains serve-only. Existing inline callback callers retain their behavior.
7. Every eligible key matches an independent baseline payload; gzip decompresses to identical raw bytes. A valid cache hit invokes neither the aggregate builder nor gzip.compress. Domain size stays bounded by two global keys plus known project count.
8. Direct SQL or older writer changes invalidate the persistent cache; a build straddling a new revision cannot publish. Cache writes cannot invalidate themselves; identical payloads cause zero large BLOB updates.
9. Unknown parameters/project, fresh=1, custom operator limit and dynamic endpoints preserve current behavior. Time advancement and live CP state still update the non-domain endpoints through the existing approximately ten-second refresh path.
10. Cache builder/publish/format failures leave real HTTP reads available. The eight deployed connection regressions remain green; no cross-thread SQLite handle sharing is introduced.
11. Run applicable related unit/integration/contract suites, syntax/diff checks and independent Spec/Cleaner/Architect review. Record all failures; two agent-model detection failures and two platform skips are known baseline results, not grounds for weakening tests.
12. On isolated fixtures, record baseline and optimized runtime mirror DML, WAL growth and CLI duration; stable optimized mirrors must have zero DML. Through the real merged-server entrypoint, six concurrent clients complete 600 representative GETs with no errors while a collect round is active, within a 30-second request bound. Report P50/P95, serving/child CPU and post-load ten-second average idle CPU (target <5% of one core). Measure cache-hit requests separately against baseline and prove the eliminated aggregate/compression work; do not reuse the attachment's historical timings as measurements.
13. Real failure QA covers a held collection lock, child timeout, cache miss, stale build fencing and restart with an already initialized database. Temporary processes/files are cleaned up and AC evidence is persisted.

Final isolated baseline/optimized measurements and validation limits: [dashboard performance QA](../qa/2026-10-24-code-loop-dashboard-performance.md).
