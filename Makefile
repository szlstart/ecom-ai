CONDA_ENV := ecom-ai
PYTHON := /opt/miniconda3/envs/$(CONDA_ENV)/bin/python
PIP := $(PYTHON) -m pip

.PHONY: bootstrap install lock lint format test registry-check migrate infra-up infra-down app-up app-down api frontend check

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
	cd backend && ../.scripts/compile-requirements.sh

lint:
	cd backend && $(PYTHON) -m ruff check app tests
	cd backend && $(PYTHON) -m mypy app
	cd frontend && pnpm typecheck

format:
	cd backend && $(PYTHON) -m ruff format app tests
	cd backend && $(PYTHON) -m ruff check --fix app tests

test:
	cd backend && $(PYTHON) -m pytest

registry-check:
	$(PYTHON) scripts/validate_registries.py

migrate:
	cd backend && $(PYTHON) -m alembic -c alembic.mysql.ini upgrade head
	cd backend && $(PYTHON) -m alembic -c alembic.postgres.ini upgrade head

infra-up:
	docker compose up -d mysql postgres redis

infra-down:
	docker compose down

app-up:
	docker compose --profile app up -d --build

app-down:
	docker compose --profile app down

api:
	cd backend && $(PYTHON) -m uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port 8000

frontend:
	cd frontend && pnpm dev

check: registry-check lint test
