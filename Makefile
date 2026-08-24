CONDA_ENV ?= ecom-ai
CONDA_PYTHON := /opt/miniconda3/envs/$(CONDA_ENV)/bin/python
PYTHON ?= $(if $(wildcard $(CONDA_PYTHON)),$(CONDA_PYTHON),python)
PIP := $(PYTHON) -m pip

.PHONY: bootstrap install lock lint format test build registry-check openapi migrate seed admin-bootstrap infra-up infra-down app-up app-down observability-up observability-down evaluate-agent api frontend check

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

build:
	cd frontend && pnpm build

registry-check:
	$(PYTHON) scripts/validate_registries.py

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

evaluate-agent:
	./scripts/evaluate-agent.py eval/golden.json

api:
	cd backend && $(PYTHON) -m uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000

frontend:
	cd frontend && pnpm dev

check: registry-check openapi lint test build
