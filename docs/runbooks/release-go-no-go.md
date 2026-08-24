# Release Go/No-Go Evidence

Run this checklist in an isolated release environment and attach the generated artifacts. A script existing is not evidence of a successful drill.

## Required evidence

- `make lint test build` and both migration chains from empty and previous-version fixtures.
- Signed image digest, SBOM, and a clean HIGH/CRITICAL scan (`scripts/sbom-scan.sh`).
- Canary expansion observations and automatic rollback output (`scripts/canary-rollback.sh`).
- Encrypted MySQL/PostgreSQL backups (`scripts/backup-create.sh`), SHA256 manifest (`scripts/backup-checksum.sh`), isolated restore, row/invariant reconciliation, and measured RPO/RTO.
- Object versioning/replication evidence from `scripts/object-replication-check.sh` for originals and receipts.
- Load, stress, spike, soak, and chaos reports with P95/P99, errors, pool/lock/queue headroom (`scripts/performance-report.sh`).
- Golden/evaluation output with case-level diffs, security holdout, latency/cost and a release gate. Contract-only output remains insufficient for model-quality claims.

## Automatic no-go conditions

- Any migration, invariant, ACL, security holdout, backup checksum/restore, or rollback failure.
- Missing trace/run/tool correlation or unsanitized sensitive data in telemetry.
- P99/SLO breach without 30% capacity headroom, or unresolved dependency `unknown` writes.
- Real payment, refund, order-write, or unrestricted AI Tool enabled in the release.

## Release sequence

1. Run `make release-preflight` against the secret-manager-rendered `.env.production` file.
2. Apply `mysql-migration-job` and then `postgres-migration-job`; record both previous and resulting heads.
3. Deploy the digest-pinned canary, collect business and technical SLOs, and exercise the rollback script with the previous digest.
4. Run backup restore and object replication drills in isolated accounts. Record observed RPO and RTO against the component-specific targets in design section 3.32.14 (for example, MySQL RPO at most 15 minutes and RTO at most 60 minutes).
5. Run all four k6 scenarios and one dependency-loss chaos drill. Capacity is accepted only with at least 30% headroom for connections, disk, queues, workers, Agent concurrency, and model spend.
6. Complete the signed Go/No-Go review. Keep real payment and AI write Skills disabled until every required artifact is attached and reviewed.

The MySQL automated rollback floor is revision `k05f6a8b9c0d`. Crossing that boundary would recreate retired plaintext internal-note storage, so the migration deliberately fails closed. Older-version recovery uses encrypted backup/PITR plus a reviewed forward fix; CI tests `head → k05 → head` and PostgreSQL `head → base → head`.

The repository ships the controls and repeatable commands, but an unexecuted command is never release evidence. Missing credentials, infrastructure, scanners, load generators, restore targets, or reports therefore produce **No-Go**, not a waived result.
