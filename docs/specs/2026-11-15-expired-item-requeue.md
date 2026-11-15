# Expired work item requeue

## Goal and scope
When an execution ends without published effects, return its work item to READY only if the latest committed project snapshot still contains that exact key and version. If it disappeared or changed, cancel the old item. Apply this consistently to expiration, idle collapse, abandonment and resource stealing through the common store operation. A claim also cancels and skips previously stale READY candidates against the same map.

## Constraints and acceptance
Preserve fencing, transactional updates, terminal derivation and unresolved-outbox protection. A standalone store with no snapshot metadata retains its prior successor-based behavior. A later sync can remint a current item. Verify merged-PR removal, unchanged and changed versions, legacy store behavior and unresolved publication; test the real sync, expiry and claim sequence and relevant concurrent paths. No public interface or schema change.
