# Code Loop dashboard performance QA

Date: 2026-10-24
Optimized branch: `codex/2026-10-23-code-loop-dashboard-performance`
Baseline: clean `origin/main` at `99ea559f90974d42e70c6cb1673ed58407e05c71`.

## Method

Both versions ran the same temporary fixture in isolated `HOME` and `CODE_LOOP_HOME` directories. The fixture used the real merged dashboard listener factory (`cp_server._ui_listener`), three projects, 450 closed issues with repeated 8 KiB bodies, and 10,000 CP runtime rows. The serialized default `/api/items` payload was 3,876,017 bytes; its gzip representation was 31,868 bytes.

An ephemeral HTTP platform fixture delayed the three project branch lookups for 25 seconds. After the real UI collect request reached those lookups, six concurrent clients made 100 requests each: 480 gzip `/api/items` reads and 120 `/api/runtime?project=...` reads. The collect status was `running` both when load started and after all 600 requests. Both runs returned HTTP 200 for every request with no errors. The local fixture and temporary files were removed after each run.

The temporary benchmark instrumented the dashboard handler with `time.thread_time()` to isolate server-thread CPU. Its process-wide `RUSAGE_SELF` also includes Python's load-generator threads and is not used as a serving-CPU comparison. Runtime-table DML was counted with temporary SQLite triggers during the first collect round. Those triggers were then removed, the WAL was truncated, and a second unchanged collect measured WAL bytes without counter-trigger writes.

## Results

| Measurement | clean `origin/main` | optimized | Change |
| --- | ---: | ---: | ---: |
| 600-request elapsed time | 0.730 s | 0.564 s | 22.7% lower |
| Request P50 | 8.415 ms | 6.102 ms | 27.5% lower |
| Request P95 | 9.258 ms | 8.035 ms | 13.2% lower |
| Dashboard handler CPU for 600 GETs | 4.024 s | 2.102 s | 47.8% lower |
| Runtime-mirror DML on unchanged round | 20,000 | 0 | eliminated |
| Uninstrumented WAL after unchanged round | 2,261,912 B | 57,712 B | 97.4% lower |
| `/api/items` gzip calls during load | 480 | 0 | eliminated |
| `/api/items` builder calls during load | 0 | 0 | unchanged |
| Post-load idle process CPU over 10 s | 0.0306% of one core | 0.0292% of one core | both below 5% target |

The baseline already served `/api/items` from its ten-second in-memory cache during the load window, so it performed no builder calls there, but it compressed the body for 480 responses. Before load, the optimized persistent cache served both plain and gzip items responses without invoking the builder or compressor; the baseline built and compressed once on the cold request. The optimized collect ran in a child process and used 0.248 CPU seconds for the delayed round. Baseline collection ran inline in the merged server, so it has no separately measurable child CPU. A subsequent unchanged round took 0.272 s optimized and 0.333 s baseline wall time.

The near-zero idle percentages describe this service process after the isolated load; this fixture does not measure whole-machine CPU, physical temperature, or production traffic.

## Validation and limits

- Nine related dashboard, collector, connection, refresh, serving, Operator wiring and server suites: 286 tests passed after the final numeric-ID normalization (`python3 -m unittest tests.test_dashboard_store tests.test_dashboard_collect tests.test_dashboard_serve tests.test_dashboard_connections tests.test_dashboard_collect_async tests.test_dashboard_refresh tests.test_dashboard_serving tests.test_operator_wiring tests.test_cp_server`, from `services/code-loop`).
- The independent populated golden payloads, cache revision fencing, fallback, process deadline/output bounds, same-database lock contention, per-database concurrency, nested transaction ownership and rollback are covered by the related suites.
- Independent review found and helped resolve exact lease identity, failed-cache-read fallback, project invalidation scope and large numeric CP ID precision findings. The reviewer reported no remaining blocking finding after the fixes.
- The measurement host was macOS. POSIX kernel-lock behavior was exercised with real local processes; the Windows `msvcrt` branch was reviewed but not run on a Windows host.
- CP IDs use SQLite `INTEGER` affinity in the mirror schema. Numeric CP IDs must be integral and within SQLite's signed 64-bit range; out-of-range numeric or fractional IDs fail the project projection atomically rather than being rounded to a colliding REAL value. Long numeric ID identity normalization itself is exact and independent of the active Decimal context.
- These numbers compare this isolated software workload only. They do not establish a machine-wide CPU or temperature change.
