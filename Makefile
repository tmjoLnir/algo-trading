.DEFAULT_GOAL := help
.PHONY: help install up up-prod deploy down logs migrate revision seed backfill test test-unit \
        test-integration lint typecheck fmt check check-bindings gen-types build-web \
        dev-api dev-worker dev-web clean

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
	@echo "worker → market-data ingestion + scheduled jobs"
	@echo "         it places no orders yet; set WORKER_SYMBOLS to give it a watchlist"

up-prod: .env check-bindings  ## Start the stack with the BUILT dashboard behind nginx
	@# `web` is the dev server and stays that way; this starts `web-prod`
	@# instead, which is the bundle `npm run build` produces served by nginx
	@# with /api and /ws proxied onto the same origin. Services are named
	@# explicitly so the dev server does not come up alongside it on 5173.
	docker compose --profile prod up -d --build db redis api worker web-prod
	@# Asked of compose rather than recomputed here. ATP_WEB_BIND_ADDR is
	@# usually set in .env, which compose reads for interpolation and make does
	@# not — so a message assembled from make's own environment would confidently
	@# report the wrong address exactly when it matters.
	@bound=$$(docker compose --profile prod port web-prod 80 2>/dev/null); \
	 bound=$${bound:-127.0.0.1:8080}; \
	 echo; \
	 echo "dashboard → http://$$bound   (built bundle; API on the same origin)"; \
	 echo "api       → http://127.0.0.1:8000/docs   (loopback only, by design)"; \
	 echo; \
	 case "$$bound" in \
	   127.0.0.1:*|localhost:*) \
	     echo "   Reachable from this host only. To share it, set ATP_WEB_BIND_ADDR in"; \
	     echo "   .env to a LAN or VPN address — docs/DASHBOARD.md, 'Reaching it from"; \
	     echo "   another machine'.";; \
	   *) \
	     echo "!! NO AUTHENTICATION (ROADMAP Phase 6). Anyone who reaches $$bound reads"; \
	     echo "!! the entire book and can call the risk endpoints. check-bindings has"; \
	     echo "!! confirmed that address is private; keep the network it is on trusted.";; \
	 esac

deploy: check-bindings  ## Deploy on a host: built dashboard, code from the images
	@# Not `up-prod`, which is the built dashboard on top of the *development*
	@# stack — source bind-mounted over the image, uvicorn reloading, and no
	@# restart policy on the database. This applies docker-compose.prod.yml as
	@# well, which is the difference between a demo and a deployment. Why this
	@# target exists and what it is deploying onto: ADR 0011, docs/DEPLOYMENT.md.
	@#
	@# Deliberately no `.env` prerequisite, unlike `up` and `up-prod`. On a
	@# developer's machine writing one from .env.example is a convenience; on a
	@# host it is the deployment's entire configuration, and conjuring one that
	@# says ATP_RUN_MODE=backtest with no credentials would be a silent wrong
	@# answer. Compose names what is missing instead.
	@#
	@# `up -d` recreates a changed container by stopping the old one before
	@# starting the new — it never runs both. That is not a detail here: Alpaca
	@# refuses a second stream connection per key, and two workers is the
	@# duplicate-position incident in docs/RUNBOOK.md. Do not replace this with
	@# anything that overlaps instances.
	docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
	@bound=$$(docker compose -f docker-compose.yml -f docker-compose.prod.yml port web-prod 80 2>/dev/null); \
	 bound=$${bound:-127.0.0.1:8080}; \
	 echo; \
	 echo "dashboard → http://$$bound"; \
	 echo; \
	 echo "   Deployed shape: code from the images, every service restarts, no"; \
	 echo "   source mounted from this checkout. Confirm what is actually running"; \
	 echo "   before you trust it — docs/DEPLOYMENT.md, 'After every deploy'."

down:  ## Stop the stack
	@# --profile prod so this also takes down web-prod, which is otherwise
	@# outside the set of services `down` considers. Covers a `make deploy`
	@# stack too: containers are removed by project name, which the overlay
	@# does not change.
	docker compose --profile prod down

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
	@# The web half had no formatting gate while the Python half did, so
	@# `make fmt` produced a 27-file diff the first time anyone ran it. There is
	@# a `.prettierrc.json` now that agrees with the code as written; this keeps
	@# it that way.
	npm --prefix $(WEB) run format:check

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

check-bindings:  ## Fail if any compose service publishes a port on every interface
	@# Not part of `check`, which deliberately needs no Docker daemon. CI runs
	@# this in the `stack` job. Plain python3 rather than `uv run`: the script
	@# is stdlib-only, and the CI job that needs it most has docker but no
	@# synced Python environment.
	python3 scripts/check_port_bindings.py

check: check-tracked lint typecheck test  ## Everything CI runs — green before you push

gen-types:  ## Regenerate TS API types from the OpenAPI schema
	@# Dumped from the app rather than fetched from a running server. Requiring
	@# `make up` and a live API to regenerate types is how hand-written
	@# duplicates of a server contract end up being maintained instead
	@# (CLAUDE.md §4).
	uv run python scripts/dump_openapi.py $(WEB)/openapi.json
	npm --prefix $(WEB) run gen:types

# ── local dev ───────────────────────────────────────────────────────────────
dev-api:
	uv run uvicorn atp_api.main:app --reload --port 8000

dev-worker:
	uv run python -m atp_worker.main

dev-web:
	npm --prefix $(WEB) run dev

build-web:  ## Build the dashboard to apps/web/dist (static files, no server)
	@# For serving the bundle from something other than this repo's nginx
	@# image. The output addresses the API on its own origin, so whatever
	@# serves dist/ must also route /api and /ws to the API — see
	@# infra/docker/web.nginx.conf for the configuration that does.
	npm --prefix $(WEB) run build

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
