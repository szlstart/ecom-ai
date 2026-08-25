from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_container_is_non_root_and_reproducible() -> None:
    dockerfile = (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")
    assert "--require-hashes" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert ":latest" not in dockerfile


def test_compose_uses_loopback_ports_and_observability_profiles() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    for service in ("mysql", "postgres", "redis", "minio"):
        for port in services[service].get("ports", []):
            assert str(port).startswith("127.0.0.1:")
    assert services["prometheus"]["profiles"] == ["observability"]
    assert services["grafana"]["profiles"] == ["observability"]
    assert services["agent-runtime-worker"]["command"][-1] == "app.workers.agent_runtime_worker"
    assert services["otel-collector"]["profiles"] == ["observability"]
    assert services["loki"]["profiles"] == ["observability"]
    assert services["tempo"]["profiles"] == ["observability"]


def test_ci_exercises_forward_backward_migrations_and_all_gates() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "alembic.mysql.ini downgrade k05f6a8b9c0d" in ci
    assert "alembic.mysql.ini downgrade base" not in ci
    assert "alembic.postgres.ini downgrade base" in ci
    assert "make lint acceptance-test build" in ci
    assert 'ECOM_RUN_FILE_INTEGRATION_TESTS: "1"' in ci
    assert "ecom-minio-ci" in ci and "ecom-clamav-ci" in ci
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "ECOM_RUN_INTEGRATION_TESTS=1" in makefile
    assert "--cov-fail-under=60" in makefile


def test_release_images_are_scanned_attested_and_keyless_signed() -> None:
    workflow = (ROOT / ".github/workflows/release-images.yml").read_text(encoding="utf-8")
    assert "docker/build-push-action@v7" in workflow
    assert "sbom: true" in workflow
    assert "provenance: mode=max" in workflow
    assert "aquasecurity/trivy-action@v0.36.0" in workflow
    assert "cosign sign --yes" in workflow
    assert "steps.build.outputs.digest" in workflow
    assert ":latest" not in workflow


def test_operational_drills_require_encryption_replication_and_restore() -> None:
    preflight = (ROOT / "scripts/backup-preflight.sh").read_text(encoding="utf-8")
    assert "BACKUP_ENCRYPTION_KEY_ID" in preflight
    assert "OBJECT_REPLICATION_TARGET" in preflight
    restore = (ROOT / "scripts/backup-restore-drill.sh").read_text(encoding="utf-8")
    assert "pg_restore" in restore and "mysql" in restore
    smoke = (ROOT / "scripts/load-smoke.sh").read_text(encoding="utf-8")
    assert "CONCURRENCY" in smoke
    performance = (ROOT / "scripts/performance-scenarios.js").read_text(encoding="utf-8")
    for scenario in ("load", "stress", "spike", "soak"):
        assert f"{scenario}:" in performance
    assert '"p(95)<1000"' in performance
    assert '"p(99)<2000"' in performance


def test_production_override_removes_data_ports_and_has_migration_job() -> None:
    production = (ROOT / "compose.production.yaml").read_text(encoding="utf-8")
    assert "ports: !reset []" in production
    assert "must be pinned by sha256 digest" in production
    assert "mysql-migration-job:" in production
    assert "postgres-migration-job:" in production
    assert "profiles: [local-data]" in production
    assert "env_file: !override [.env.production]" in production
    assert "build: !reset null" in production
    supply_chain = (ROOT / "scripts/sbom-scan.sh").read_text(encoding="utf-8")
    assert "syft" in supply_chain and "trivy" in supply_chain and "cosign" in supply_chain
    canary = (ROOT / "scripts/canary-rollback.sh").read_text(encoding="utf-8")
    assert "ROLLBACK_IMAGE" in canary and "--force-recreate" in canary
    release_preflight = (ROOT / "scripts/release-preflight.py").read_text(encoding="utf-8")
    assert "DIGEST_IMAGE" in release_preflight
    assert "rediss://" in release_preflight


def test_local_file_dependency_images_are_digest_pinned() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "minio/minio@sha256:" in compose
    assert "clamav/clamav-debian@sha256:" in compose
