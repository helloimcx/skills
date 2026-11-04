# Module Architecture Index

This index links architecture documents for larger independent modules added or changed under the repository's current documentation rules.

| Module | Responsibility | Architecture |
|---|---|---|
| `code-loop` | CP-side global Operator routing, durable machine tasks, resident WorkerAgent ingress, per-task worker-local Coordinator and independent Handler; loopback dashboard with supervised collection and revision-fenced serving cache | [Code Loop](modules/code-loop.md) |
| `project-setup` | Independently initialize, establish durable cross-Agent architecture governance and hard static-quality gates, and cold-start verify new or agreed bootstrap-only software projects | [Project Setup](modules/project-setup.md) |

Each module document describes current responsibilities, public interfaces, dependency boundaries, and core flow. Update the relevant document in the same change when those facts change.
