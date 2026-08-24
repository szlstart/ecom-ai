# On-call and Incident Command

## Ownership and escalation

Every production service, queue, dependency and alert must have a named primary owner, secondary owner, business owner and escalation channel in the deployment service catalog. A production release is No-Go while any P0/P1 alert has no reachable owner. Personal phone numbers, access tokens and credentials stay in the approved paging/secret system, not this repository.

| Severity | Example | Acknowledge | Incident command |
| :--- | :--- | :--- | :--- |
| P0 | unauthorized disclosure, payment/inventory invariant failure, destructive data event | 5 min | immediately; security/business/engineering present |
| P1 | checkout unavailable, sustained critical SLO burn, backup/PITR failure | 10 min | within 15 min |
| P2 | degraded non-critical path with a safe fallback | 30 min | owner-led |
| P3 | warning or capacity trend with no current user impact | next business day | ticket |

## Response loop

1. Acknowledge and name the incident commander, operations lead and communications lead.
2. Preserve evidence (`trace_id`, release digest, feature/Agent/Skill versions, metrics and audit references); never paste secrets or raw private content into chat.
3. Stop harm first: disable the affected write Skill/provider, freeze rollout, shed low-priority traffic or invoke the verified rollback.
4. Establish the last known-good time and classify every external write timeout as `unknown` until reconciled.
5. Recover using the service runbook, verify business invariants, then reopen traffic in controlled steps.
6. Publish an incident timeline, impact, root cause and owned corrective actions. P0/P1 actions require a regression or drill before closure.

Hand-off between shifts includes active symptoms, exact mitigation state, unresolved `unknown` writes, next decision time and links to immutable evidence. An alert is not closed merely because metrics recovered.
