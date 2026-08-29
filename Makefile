CONDA_ENV ?= ecom-ai
CONDA_PYTHON := /opt/miniconda3/envs/$(CONDA_ENV)/bin/python
PYTHON ?= $(if $(wildcard $(CONDA_PYTHON)),$(CONDA_PYTHON),python)
PIP := $(PYTHON) -m pip
ACCEPTANCE_ENV := ECOM_ENVIRONMENT=testing ECOM_RUN_INTEGRATION_TESTS=1 ECOM_RUN_FILE_INTEGRATION_TESTS=1 ECOM_OBJECT_STORAGE_ENABLED=true ECOM_OBJECT_STORAGE_ACCESS_KEY=$${ECOM_OBJECT_STORAGE_ACCESS_KEY:-local-minio-admin} ECOM_OBJECT_STORAGE_SECRET_KEY=$${ECOM_OBJECT_STORAGE_SECRET_KEY:-local-minio-change-me} ECOM_OBJECT_STORAGE_BUCKET_PREFIX=$${ECOM_OBJECT_STORAGE_BUCKET_PREFIX:-test-acceptance-} ECOM_FILE_SCANNER_ENABLED=true ECOM_FILE_SCANNER_HOST=$${ECOM_FILE_SCANNER_HOST:-127.0.0.1} ECOM_FILE_SCANNER_PORT=$${ECOM_FILE_SCANNER_PORT:-13310}
BACKEND_RUNTIME_SERVICES := api file-worker lifecycle-worker batch-worker order-timeout-worker payment-reconcile-worker logistics-sync-worker admin-approval-worker realtime-outbox-worker agent-runtime-worker knowledge-indexer ai-memory-cleanup-worker account-deletion-worker
APP_RUNTIME_SERVICES := $(BACKEND_RUNTIME_SERVICES) frontend

.PHONY: bootstrap install lock format trace-catalog lint test acceptance-test acceptance-test-isolated acceptance-evidence agent-security-test security-check build registry-check acceptance-audit acceptance-gate go-no-go-validate openapi migrate seed admin-bootstrap merchant-bootstrap dev-clean-test-admins infra-up infra-down app-up app-down observability-up observability-down backup-preflight backup-create backup-restore-drill object-replication-check load-smoke performance-report sbom-scan canary-rollback release-preflight evaluate-agent api frontend check

bootstrap:
	/opt/miniconda3/bin/conda env update --name $(CONDA_ENV) --file environment.yml --prune
	$(PIP) install pip-tools==7.6.1
	$(MAKE) lock
	$(MAKE) install

install:
	$(PIP) install --require-hashes -r backend/requirements/dev.txt
	$(PIP) install --no-deps -e backend
	cd frontend && pnpm install --frozen-lockfile

lock:
	cd backend && PYTHON_BIN="$(PYTHON)" ../.scripts/compile-requirements.sh

lint:
	cd backend && $(PYTHON) -m ruff check app tests
	cd backend && $(PYTHON) -m mypy app tests
	cd frontend && pnpm typecheck

format:
	cd backend && $(PYTHON) -m ruff format app tests
	cd backend && $(PYTHON) -m ruff check --fix app tests

test:
	cd backend && $(PYTHON) -m pytest
	cd frontend && pnpm test

acceptance-test: build
	mkdir -p artifacts/acceptance/current/quality artifacts/acceptance/current/database artifacts/acceptance/current/agent
	cd backend && $(ACCEPTANCE_ENV) $(PYTHON) -m app.core.testing_safety
	cd backend && $(PYTHON) -m alembic -c alembic.mysql.ini current > ../artifacts/acceptance/current/database/mysql-schema-drift.txt
	cd backend && $(PYTHON) -m alembic -c alembic.mysql.ini check >> ../artifacts/acceptance/current/database/mysql-schema-drift.txt 2>&1
	cd backend && $(PYTHON) -m alembic -c alembic.postgres.ini current > ../artifacts/acceptance/current/database/postgres-schema-drift.txt
	cd backend && $(PYTHON) -m alembic -c alembic.postgres.ini check >> ../artifacts/acceptance/current/database/postgres-schema-drift.txt 2>&1
	cd backend && $(ACCEPTANCE_ENV) $(PYTHON) -m pytest --junitxml=../artifacts/acceptance/current/quality/backend-junit.xml --cov=app --cov-fail-under=60 --cov-report=term:skip-covered --cov-report=xml:../artifacts/acceptance/current/quality/backend-coverage.xml
	cd frontend && pnpm test --reporter=junit --outputFile=../artifacts/acceptance/current/quality/frontend-junit.xml
	cd frontend && pnpm test:e2e
	$(MAKE) agent-security-test
	$(PYTHON) scripts/evaluate-agent.py eval/golden.json --allow-missing-observations --output artifacts/acceptance/current/agent/evaluation-report.json
	$(MAKE) acceptance-evidence

# Preferred local entry point: exact test-only database/cache/object namespaces
# are created and removed even when a test fails. CI already supplies isolated
# service containers and therefore continues to call acceptance-test directly.
acceptance-test-isolated:
	./scripts/run-isolated-acceptance.sh

acceptance-evidence:
	$(PYTHON) scripts/generate_acceptance_evidence.py

agent-security-test:
	mkdir -p artifacts/acceptance/current/agent
	cd backend && $(ACCEPTANCE_ENV) $(PYTHON) -m pytest --junitxml=../artifacts/acceptance/current/agent/security-tests.xml tests/test_prompt_safety.py tests/test_store_agent_integration.py::test_store_agent_scope_context_tools_and_handoff tests/test_exclusive_agent_integration.py::test_exclusive_agent_refund_requires_consent_and_button_approval tests/test_knowledge_postgres_integration.py::test_shadow_index_and_acl_filtered_keyword_retrieval tests/test_knowledge_postgres_integration.py::test_agent_retrieval_rechecks_trusted_scope_version_and_publication tests/test_ai_privacy_api_integration.py::test_ai_memory_owner_revision_tombstone_disable_and_retry tests/test_operations_agent_integration.py::test_admin_copilot_runs_bounded_parallel_read_only_specialists

security-check:
	SECURITY_PYTHON="$(PYTHON)" SECURITY_IMAGES="$${SECURITY_IMAGES:-ecom-ai-api:local ecom-ai-frontend:latest}" ./scripts/security-check.sh

build:
	cd frontend && pnpm build

registry-check:
	$(PYTHON) scripts/validate_registries.py

trace-catalog:
	PYTHONPATH=backend $(PYTHON) scripts/generate_operation_trace_catalog.py

acceptance-audit:
	$(PYTHON) scripts/acceptance-audit.py

acceptance-gate:
	$(PYTHON) scripts/acceptance-audit.py --strict

go-no-go-validate:
	@test -n "$(MANIFEST)" || (echo "Usage: make go-no-go-validate MANIFEST=artifacts/acceptance/<release>/go-no-go.json" && exit 2)
	$(PYTHON) scripts/validate-go-no-go.py "$(MANIFEST)"

openapi: trace-catalog
	PYTHONPATH=backend $(PYTHON) scripts/export_openapi.py
	cd frontend && pnpm generate:api

migrate:
	cd backend && $(PYTHON) -m alembic -c alembic.mysql.ini upgrade head
	cd backend && $(PYTHON) -m alembic -c alembic.postgres.ini upgrade head

seed:
	cd backend && $(PYTHON) -m app.bootstrap.cli

admin-bootstrap:
	@test -n "$(USERNAME)" || (echo "Usage: make admin-bootstrap USERNAME=<admin_username>" && exit 2)
	cd backend && $(PYTHON) -m app.bootstrap.admin_cli "$(USERNAME)"

merchant-bootstrap:
	@test -n "$(USERNAME)" || (echo "Usage: make merchant-bootstrap USERNAME=<merchant_username> STORE_NAME=<store_name>" && exit 2)
	@test -n "$(STORE_NAME)" || (echo "Usage: make merchant-bootstrap USERNAME=<merchant_username> STORE_NAME=<store_name>" && exit 2)
	cd backend && $(PYTHON) -m app.bootstrap.merchant_cli "$(USERNAME)" --store-name "$(STORE_NAME)"

# Preview by default. Pass APPLY=1 only after checking the exact usernames.
dev-clean-test-admins:
	cd backend && $(PYTHON) -m app.bootstrap.development_cleanup $(if $(filter 1,$(APPLY)),--apply,)

infra-up:
	docker compose up -d mysql postgres redis minio clamav

infra-down:
	docker compose down

app-up:
	docker compose up -d --wait mysql postgres redis minio clamav
	ECOM_BUILD_SHA=$$(git rev-parse HEAD) docker compose --profile app build api frontend
	docker compose --profile app up -d --no-deps --no-build --force-recreate $(APP_RUNTIME_SERVICES)
	$(PYTHON) scripts/verify_runtime_builds.py

app-down:
	docker compose --profile app down

observability-up:
	docker compose --profile app --profile observability up -d tempo loki otel-collector prometheus grafana

observability-down:
	docker compose --profile observability stop grafana prometheus otel-collector loki tempo

backup-preflight:
	./scripts/backup-preflight.sh

backup-create:
	./scripts/backup-create.sh

backup-restore-drill:
	./scripts/backup-restore-drill.sh

object-replication-check:
	./scripts/object-replication-check.sh

load-smoke:
	./scripts/load-smoke.sh

performance-report:
	./scripts/performance-report.sh

sbom-scan:
	./scripts/sbom-scan.sh

canary-rollback:
	./scripts/canary-rollback.sh

release-preflight:
	$(PYTHON) scripts/release-preflight.py

evaluate-agent:
	mkdir -p artifacts/acceptance/current/agent
	$(PYTHON) scripts/evaluate-agent.py eval/golden.json $(if $(AGENT_OBSERVATIONS),--observations $(AGENT_OBSERVATIONS),) --output artifacts/acceptance/current/agent/evaluation-report.json

api:
	cd backend && $(PYTHON) -m uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000

frontend:
	cd frontend && pnpm dev

check: registry-check openapi lint test build
