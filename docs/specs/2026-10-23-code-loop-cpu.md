# Code Loop dashboard CPU and connection ownership

Status: Approved by the user on 2026-10-23 (Asia/Shanghai).
Complexity: Medium — the defect spans HTTP handlers, background snapshot refresh and SQLite connection construction; database schema, CP transaction and network contracts stay compatible.

## Goal
Remove concurrent use of one dashboard SQLite connection, the confirmed unsafe pattern associated with the resident cp_server process consuming about 600% CPU.

## Evidence and limits
- 2026-10-23 22:46–22:49 Asia/Shanghai: PID 46998, cp_server.py serve, repeatedly measured at 597–598% CPU.
- The two-second macOS sample has six threads spending nearly all samples in sqlite3_reset → sqlite3VdbeReset → sqlite3_db_release_memory → purgeableCacheShrink → mach_vm_purgable_control.
- The serving interpreter is Apple Python 3.9.6 / SQLite 3.54.0, compiled THREADSAFE=2. That mode forbids concurrent operations on one connection or its derived objects (https://sqlite.org/threadsafe.html).
- dash.serve.create closes over a single dashboard connection. Concurrent GETs, stale snapshot recomputes and POST validation all access it without a database lock. schema.connect disables the Python thread-affinity guard and incorrectly assumes serialized mode.
- This proves an unsafe connection-sharing defect; it does not prove exactly when internal SQLite cache state became invalid. Native infinite-loop reproduction is nondeterministic and must not be required for a regression test.
- Operator is disabled on the affected installation. Changes to operator work-item transactions are outside this fix.

## Scope
- A read-only, thread-affine dashboard connection opener with deterministic cleanup.
- Production HTTP queries, POST validation and snapshot recomputes each acquire their own connection for one complete database operation.
- Preserve create(db, ...) callers with an application lock protecting the complete read/materialization when a connection factory is absent.
- Wire the merged server to the factory and correct the misleading connection-safety documentation.
- Regression tests for ownership, overlap, cleanup, cached background refresh, read-only behavior and existing response contracts; isolated real HTTP concurrency and CPU measurements.

## Non-goals
- No platform crawling cadence changes, database migration, Python/SQLite replacement, frontend changes, CP transaction redesign or unrelated optimization.
- No deployment, restart or modification of the running daemon in this proposal. A fixed checkout must be explicitly deployed and the old process restarted before its native stuck threads can disappear.

## Behavior / Interface
- Existing HTTP URLs, payloads, status codes, cache TTL, invalidation and background collection behavior remain compatible.
- create gains an optional trailing db_factory callable. Factory connections are owned by the operation and always closed; the supplied legacy connection remains caller-owned.
- Production read connections open an existing file with mode=ro, row_factory=sqlite3.Row, autocommit and default Python thread affinity. They do not initialize schemas or change journal settings.
- The existing initial dashboard connection initializes schema and remains available for the optional consumer; HTTP reads no longer use it.

## Constraints / Compatibility
Pure Python 3.9+ standard library, Windows/macOS/Linux. Preserve WAL, busy timeouts and write callbacks with their existing separate connection ownership. Preserve the eight pre-existing dirty files in the host checkout; changes are based on origin/master in the task worktree.

## Acceptance Criteria
1. Deterministic concurrent-request tests fail against the old implementation and pass after the change: each factory connection is created, queried, fully materialized and closed on the same thread, and no two operations use one connection concurrently.
2. Stale snapshot refresh and uncached requests use independent factory connections; success and exception paths close every acquired connection.
3. Legacy supplied-connection calls remain usable, serialize complete query/materialization sections and do not close the caller connection.
4. POST project/issue validation uses the same safe acquisition discipline; existing endpoint and snapshot invalidation tests pass.
5. Read-only opener rejects writes and missing files without creating a database; path encoding works for special characters.
6. Relevant dashboard/merged-server regression suites pass, followed by the existing service suite if the environment supports it; any unrelated baseline failures are documented.
7. An isolated merged-server HTTP run completes six concurrent clients with 100 requests each within a 30-second bound on a fixed local fixture, reports latency and process CPU time, and uses less than 5% of one core during a subsequent three-second idle interval. Benchmark runs in a bounded subprocess so failures cannot leave a hot daemon.
8. Independent review has no unresolved Critical/High finding, and documentation matches the final connection lifecycle.

## QA-driven correction (2026-10-23)
Real merged-server QA with PR numbers matching project ids exposed an existing kanban verdict-map lookup bug: the outer {project: {number: verdict}} map was passed where one project's map was expected, yielding HTTP 400 (KeyError 0). The minimal correction selects verdicts[pid] in dash/views.py; add a real HTTP regression in test_dashboard_connections.py before repeating performance QA and independent review. No response contract or database schema changes. This correction is necessary to exercise the approved representative HTTP main path.
