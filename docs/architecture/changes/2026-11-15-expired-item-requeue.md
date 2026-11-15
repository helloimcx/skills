# Requeue only current work items

The [plan](../../plans/2026-11-15-expired-item-requeue.md) aligns the shared execution-recovery transition with the latest committed source-version map. An expired incomplete execution for an item absent from that map or at a different version is cancelled rather than put back into the claimable queue. Current items and stores without sync metadata retain their established retry behavior; unresolved publications remain fenced.
