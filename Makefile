.DEFAULT_GOAL := help
.PHONY: help install up down logs migrate revision seed backfill test test-unit \
        test-integration lint typecheck fmt check gen-types dev-api dev-worker dev-web clean

WEB := apps/web

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## Install Python + Node dependencies
	uv sync --all-packages
	npm --prefix $(WEB) install

# ── docker ──────────────────────────────────────────────────────────────────
# A file target, so it runs exactly once — when .env is genuinely absent — and
# never overwrites a developer's filled-in credentials.
.env:
	@cp .env.example $@
	@echo "wrote $@ from .env.example — fill in ALPACA_* and API_SECRET_KEY"
	@echo "before switching ATP_RUN_MODE off backtest."

up: .env  ## Start the full stack
	docker compose up -d --build
	@echo "api  → http://localhost:8000/docs"
	@echo "web  → http://localhost:5173"
	@echo "worker is not in the default stack — see docker-compose.yml"

down:  ## Stop the stack
	docker compose down

logs:  ## Tail all service logs
	docker compose logs -f --tail=100

# ── database ────────────────────────────────────────────────────────────────
migrate:  ## Apply migrations
	uv run alembic -c infra/alembic/alembic.ini upgrade head

revision:  ## Autogenerate a migration:  make revision m="add orders table"
	uv run alembic -c infra/alembic/alembic.ini revision --autogenerate -m "$(m)"

seed:  ## Seed reference data + a sample strategy
	uv run python scripts/seed.py

backfill:  ## Backfill bars:  make backfill sym=AAPL,MSFT from=2020-01-01
	uv run python scripts/backfill_bars.py --symbols "$(sym)" --start "$(from)"

# ── quality ─────────────────────────────────────────────────────────────────
test:  ## All tests
	uv run pytest
	npm --prefix $(WEB) run test -- --run

test-unit:  ## Fast pure-Python tests only
	uv run pytest tests/unit -q

test-integration:  ## Tests needing Postgres/Redis
	uv run pytest tests/integration -m integration

lint:
	uv run ruff check .
	uv run ruff format --check .
	npm --prefix $(WEB) run lint

typecheck:
	uv run mypy libs apps
	npm --prefix $(WEB) run typecheck

fmt:  ## Auto-format everything
	uv run ruff check --fix .
	uv run ruff format .
	npm --prefix $(WEB) run format

check-tracked:  ## Fail if any source file is excluded by .gitignore
	@# CI only ever sees committed files, so an over-broad ignore rule looks
	@# like a passing local build and a broken pipeline. An unanchored `data/`
	@# once hid the whole market-data package this way. Catch it before pushing.
	@# Scoped to the actual source roots — node_modules and .venv are ignored
	@# on purpose and must not trip this.
	@ignored=$$(git ls-files --others --ignored --exclude-standard \
	    -- 'libs/core/src/**/*.py' 'apps/api/src/**/*.py' 'apps/worker/src/**/*.py' \
	       'tests/**/*.py' 'scripts/**/*.py' \
	       'apps/web/src/**/*.ts' 'apps/web/src/**/*.tsx' \
	       'infra/**/*.sql' 'infra/**/*.py' 'docs/**/*.md'); \
	if [ -n "$$ignored" ]; then \
	  echo "ERROR: source files excluded by .gitignore (CI will not see these):"; \
	  echo "$$ignored" | sed 's/^/  /'; \
	  echo "Anchor the offending rule with a leading slash (e.g. /data/ not data/)."; \
	  exit 1; \
	fi; \
	echo "check-tracked: no source files are gitignored"

check: check-tracked lint typecheck test  ## Everything CI runs — green before you push

gen-types:  ## Regenerate TS API types from the live OpenAPI schema
	npm --prefix $(WEB) run gen:types

# ── local dev ───────────────────────────────────────────────────────────────
dev-api:
	uv run uvicorn atp_api.main:app --reload --port 8000

dev-worker:
	uv run python -m atp_worker.main

dev-web:
	npm --prefix $(WEB) run dev

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
