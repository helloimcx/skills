# Handler reporting and independent review

The [implementation plan](../../plans/2026-11-14-handler-report-identity.md) changes the existing report and publication gates: Handler PR delivery may use the execution worker as reporter, while verified head and item version stay bound to the current execution. Reviewer independence and post-merge issue closure remain separate safeguards. No schema or component boundary changes.
