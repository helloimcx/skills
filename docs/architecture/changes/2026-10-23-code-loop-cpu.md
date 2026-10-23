# Dashboard read connection ownership

The previous dashboard handler shared one SQLite connection across HTTP request threads and background snapshot recomputes. This violated the connection contract of the affected Apple SQLite build (THREADSAFE=2); six sampled threads spent almost all their time in its native cache shrink path while the daemon consumed about 600% CPU.

Production reads now acquire a fresh read-only connection for one complete database operation and close it in a finally block on the same thread. POST validation follows the same discipline; action callbacks keep their independent write connections. The legacy supplied-connection API serializes complete reads without taking ownership of the connection. No database schema, HTTP payload, cache policy or platform crawling schedule changes.

The [approved plan's Expected / Delta diagram](../../plans/2026-10-23-code-loop-cpu.md#expected--delta) describes the intended flow. [Current module architecture](../modules/code-loop.md) describes the implementation. The repository uses Markdown architecture documents and has no applicable JSON architecture schema or lint:arch command.

The unsafe sharing pattern is established; the exact native corruption trigger is not. The old process must apply the fix and restart to release already stuck native threads.

Real HTTP QA also exposed a pre-existing kanban lookup error for PR numbers equal to project ids. The query now selects the per-project verdict map before deriving each PR verdict; a regression asserts that this returns HTTP 200. This restores the existing response contract without changing the board data model.
