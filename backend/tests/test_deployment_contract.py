import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_container_is_non_root_and_reproducible() -> None:
    dockerfile = (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")
    assert "--require-hashes" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert ":latest" not in dockerfile


def test_spa_document_has_browser_security_headers() -> None:
    nginx = (ROOT / "frontend/nginx.conf").read_text(encoding="utf-8")
    for directive in (
        "Content-Security-Policy",
        "frame-ancestors 'none'",
        "X-Frame-Options",
        "X-Content-Type-Options",
        "Referrer-Policy",
        "Permissions-Policy",
    ):
        assert directive in nginx
    assert "add_header_inherit merge" in nginx


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
    for service in (
        "file-worker",
        "lifecycle-worker",
        "batch-worker",
        "order-timeout-worker",
        "payment-reconcile-worker",
        "logistics-sync-worker",
        "admin-approval-worker",
        "realtime-outbox-worker",
        "agent-runtime-worker",
        "knowledge-indexer",
        "ai-memory-cleanup-worker",
        "account-deletion-worker",
    ):
        assert services[service]["healthcheck"]["test"][-1] == "--check"


def test_local_app_up_recreates_and_verifies_every_backend_runtime() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    verifier = (ROOT / "scripts/verify_runtime_builds.py").read_text(encoding="utf-8")
    assert "--force-recreate $(APP_RUNTIME_SERVICES)" in makefile
    assert "scripts/verify_runtime_builds.py" in makefile
    for service in (
        "api",
        "file-worker",
        "lifecycle-worker",
        "batch-worker",
        "order-timeout-worker",
        "payment-reconcile-worker",
        "logistics-sync-worker",
        "admin-approval-worker",
        "realtime-outbox-worker",
        "agent-runtime-worker",
        "knowledge-indexer",
        "ai-memory-cleanup-worker",
        "account-deletion-worker",
    ):
        assert f'"{service}"' in verifier


def test_ci_exercises_forward_backward_migrations_and_all_gates() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "alembic.mysql.ini downgrade k05f6a8b9c0d" in ci
    assert "alembic.mysql.ini downgrade base" not in ci
    assert "alembic.postgres.ini downgrade base" in ci
    assert "make lint acceptance-test" in ci
    assert "acceptance-test: build" in makefile
    assert 'ECOM_RUN_FILE_INTEGRATION_TESTS: "1"' in ci
    assert "ecom-minio-ci" in ci and "ecom-clamav-ci" in ci
    assert "cancel-in-progress: true" in ci
    assert "actions/upload-artifact@v4" in ci
    assert "acceptance-evidence-${{ github.sha }}" in ci
    assert "PIP_INDEX_URL: https://pypi.org/simple" in ci
    assert "playwright install --with-deps chromium" in ci
    assert "ECOM_RUN_INTEGRATION_TESTS=1" in makefile
    assert "ECOM_RUN_FILE_INTEGRATION_TESTS=1" in makefile
    assert "--cov-fail-under=60" in makefile
    assert "backend-junit.xml" in makefile
    assert "frontend-junit.xml" in makefile
    assert "pnpm test:e2e" in makefile
    assert "acceptance-evidence" in makefile
    assert "mysql-schema-drift.txt" in makefile
    assert "postgres-schema-drift.txt" in makefile
    assert makefile.count("alembic") >= 4
    assert "agent-security-test" in makefile
    assert "security-tests.xml" in makefile
    assert "--allow-missing-observations" in makefile
    assert "evaluation-report.json" in makefile


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
    for profile in (
        "public-catalog",
        "user-workspace",
        "messaging-read",
        "merchant-workspace",
        "admin-workspace",
        "mixed-read",
    ):
        assert profile in performance
    assert '"p(95)<1000"' in performance
    assert '"p(99)<2000"' in performance


def test_production_override_removes_data_ports_and_has_migration_job() -> None:
    production = (ROOT / "compose.production.yaml").read_text(encoding="utf-8")
    assert "ports: !reset []" in production
    assert "must be pinned by sha256 digest" in production
    assert "mysql-migration-job:" in production
    assert "postgres-migration-job:" in production
    assert "ECOM_MYSQL_MIGRATION_DSN" in production
    assert "ECOM_POSTGRES_MIGRATION_DSN" in production
    assert "profiles: [local-data]" in production
    assert "env_file: !reset []" in production
    assert "build: !reset null" in production
    # Keep resets explicit per service: older supported Compose releases do not
    # consistently apply tagged reset values inherited only through YAML anchors.
    assert production.count("depends_on: !reset {}") >= 12
    assert production.count("env_file: !reset []") >= 11
    assert production.count("build: !reset null") >= 12
    supply_chain = (ROOT / "scripts/sbom-scan.sh").read_text(encoding="utf-8")
    assert "syft" in supply_chain and "trivy" in supply_chain and "cosign" in supply_chain
    canary = (ROOT / "scripts/canary-rollback.sh").read_text(encoding="utf-8")
    assert "ROLLBACK_IMAGE" in canary and "--force-recreate" in canary
    assert 'SERVICE="${SERVICE:-backend}"' in canary
    assert "ai-memory-cleanup-worker" in canary
    release_preflight = (ROOT / "scripts/release-preflight.py").read_text(encoding="utf-8")
    assert "DIGEST_IMAGE" in release_preflight
    assert "rediss://" in release_preflight


def test_local_file_dependency_images_are_digest_pinned() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "minio/minio@sha256:" in compose
    assert "clamav/clamav-debian@sha256:" in compose


def test_security_gate_covers_dependencies_secrets_sast_and_images() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    script = (ROOT / "scripts/security-check.sh").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/security.yml").read_text(encoding="utf-8")
    dependabot = (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")

    assert "security-check:" in makefile
    for required in ("pip_audit", "bandit", "gitleaks", "trivy", "pnpm --dir frontend audit"):
        assert required in script
    assert "SECURITY_ALLOW_MISSING_TOOLS" in script
    assert "gitleaks/gitleaks-action@v2" in workflow
    assert "github/codeql-action/analyze@v4" in workflow
    assert "package-ecosystem: pip" in dependabot
    assert "package-ecosystem: npm" in dependabot


def test_release_preflight_accepts_synthetic_tls_configuration(tmp_path: Path) -> None:
    digest = "a" * 64
    env_file = tmp_path / "release.env"
    env_file.write_text(
        "\n".join(
            (
                f"ECOM_API_IMAGE=registry.invalid/ecom/api@sha256:{digest}",
                f"ECOM_FRONTEND_IMAGE=registry.invalid/ecom/frontend@sha256:{digest}",
                "ECOM_PUBLIC_ORIGIN=https://shop.invalid",
                    "ECOM_ALLOWED_ORIGINS=https://shop.invalid",
                    "ECOM_TRUSTED_PROXY_CIDRS=10.20.0.0/24",
                    "ECOM_METRICS_ALLOWED_CIDRS=10.30.0.0/24",
                "ECOM_MYSQL_DSN=mysql+asyncmy://runtime:secret@mysql.private.invalid:3306/ecom?ssl=true",
                "ECOM_POSTGRES_DSN=postgresql+asyncpg://runtime:secret@postgres.private.invalid:5432/ecom?ssl=require",
                "ECOM_MYSQL_MIGRATION_DSN=mysql+asyncmy://migration:secret@mysql.private.invalid:3306/ecom?ssl=true",
                "ECOM_POSTGRES_MIGRATION_DSN=postgresql+asyncpg://migration:secret@postgres.private.invalid:5432/ecom?ssl=require",
                "ECOM_REDIS_URL=rediss://:secret@redis.private.invalid:6379/0",
                "ECOM_AGENT_MODEL_API_URL=https://models.private.invalid/v1/chat/completions",
                "ECOM_AGENT_MODEL_API_KEY=synthetic-model-secret",
                "ECOM_AGENT_MODEL_NAME=approved-model",
                "ECOM_ACCESS_TOKEN_SECRET=synthetic-access-secret-32-characters-long",
                "ECOM_SECURITY_HMAC_SECRET=synthetic-hmac-secret-32-characters-long",
                "ECOM_FIELD_ENCRYPTION_KEY=synthetic-versioned-base64-key",
                "ECOM_OBJECT_STORAGE_ENDPOINT=https://objects.private.invalid",
                "ECOM_OBJECT_STORAGE_PUBLIC_ENDPOINT=https://cdn.invalid",
                "ECOM_OBJECT_STORAGE_ACCESS_KEY=synthetic-object-access",
                "ECOM_OBJECT_STORAGE_SECRET_KEY=synthetic-object-secret",
                "ECOM_FILE_SCANNER_HOST=scanner.private.invalid",
                "ECOM_OTEL_EXPORTER_OTLP_ENDPOINT=https://otel.private.invalid:4317",
            )
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [str(ROOT / "scripts/release-preflight.py")],
        cwd=ROOT,
        env={**os.environ, "ENV_FILE": str(env_file)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
