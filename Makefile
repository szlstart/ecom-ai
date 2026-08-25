CONDA_ENV ?= ecom-ai
CONDA_PYTHON := /opt/miniconda3/envs/$(CONDA_ENV)/bin/python
PYTHON ?= $(if $(wildcard $(CONDA_PYTHON)),$(CONDA_PYTHON),python)
PIP := $(PYTHON) -m pip

.PHONY: bootstrap install lock format trace-catalog lint test acceptance-test build registry-check acceptance-audit acceptance-gate go-no-go-validate openapi migrate seed admin-bootstrap infra-up infra-down app-up app-down observability-up observability-down backup-preflight backup-create backup-restore-drill object-replication-check load-smoke performance-report sbom-scan canary-rollback release-preflight evaluate-agent api frontend check

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

acceptance-test:
	mkdir -p artifacts/acceptance/current/quality artifacts/acceptance/current/database
	cd backend && $(PYTHON) -m alembic -c alembic.mysql.ini current > ../artifacts/acceptance/current/database/mysql-schema-drift.txt
	cd backend && $(PYTHON) -m alembic -c alembic.mysql.ini check >> ../artifacts/acceptance/current/database/mysql-schema-drift.txt 2>&1
	cd backend && $(PYTHON) -m alembic -c alembic.postgres.ini current > ../artifacts/acceptance/current/database/postgres-schema-drift.txt
	cd backend && $(PYTHON) -m alembic -c alembic.postgres.ini check >> ../artifacts/acceptance/current/database/postgres-schema-drift.txt 2>&1
	cd backend && ECOM_RUN_INTEGRATION_TESTS=1 $(PYTHON) -m pytest --junitxml=../artifacts/acceptance/current/quality/backend-junit.xml --cov=app --cov-fail-under=60 --cov-report=term:skip-covered --cov-report=xml:../artifacts/acceptance/current/quality/backend-coverage.xml
	cd frontend && pnpm test --reporter=junit --outputFile=../artifacts/acceptance/current/quality/frontend-junit.xml

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

openapi:
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

infra-up:
	docker compose up -d mysql postgres redis minio clamav

infra-down:
	docker compose down

app-up:
	docker compose --profile app up -d --build

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
