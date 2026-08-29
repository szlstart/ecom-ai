# Chaos and Recovery Drill

Run only in an isolated staging account with fake payment/logistics providers, synthetic users, a unique namespace and a confirmed restore point. Production fault injection requires a separately approved change record.

## Required scenarios

- Add latency and then deny connections to MySQL, PostgreSQL, Redis, object storage, the model gateway and one MCP Server independently.
- Terminate API and Worker instances during an order Outbox hand-off and during an Agent read-only run.
- Pause an event consumer until queue age breaches its warning threshold, then resume and prove idempotent catch-up.
- Remove Redis state and prove authoritative revocations remain revoked while disposable state rebuilds.
- Exercise a failed canary and restore the previous immutable image digest.
- Restore encrypted database backups and a prior object version in isolated targets; compare business invariants and measured RPO/RTO.

## Stop conditions and evidence

Stop immediately on permission bypass, money/inventory inconsistency, unbounded retry, missing audit, leaked sensitive content, or unexpected external writes. Record timestamps for injection, detection, page, acknowledgement, mitigation, recovery and invariant completion. Attach P50/P95/P99, error rate, connection headroom, queue oldest age, retry/circuit state, model cost, lost/replayed events and reconciliation output.

Passing means the documented degradation occurred, the dependency recovered without duplicate side effects, all `unknown` outcomes were reconciled, and the component met section 3.32.14. A missing metric, alert, owner, restore artifact or invariant query is a failed drill.
