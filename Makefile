# Keystone — documented entry points.
#
# Some targets reference commands that land in later tickets (seed, suite).
# They are the contract; they will work as those tickets merge.

COMPOSE := docker compose -f infra/docker-compose.yml
UV      := uv --directory service
PNPM    := pnpm --dir dashboard

.DEFAULT_GOAL := help
.PHONY: help up down db-shell seed serve dash suite test lint fmt

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

## --- infrastructure -------------------------------------------------------

up: ## Start Postgres 16 + pgvector (host port 55432) and wait for healthy
	$(COMPOSE) up -d --wait

down: ## Stop the stack (keeps the data volume)
	$(COMPOSE) down

db-shell: ## Open psql inside the running Postgres container
	$(COMPOSE) exec postgres psql -U keystone -d keystone

## --- application ----------------------------------------------------------

seed: ## Generate the deterministic dataset + golden exports (dev profile)
	$(UV) run python -m recon.seed --profile dev

serve: ## Run the FastAPI service on :8000 with reload
	$(UV) run uvicorn recon.app:create_app --factory --reload --port 8000

dash: ## Run the dashboard dev server
	$(PNPM) dev

suite: ## Run the committed grading harness and print the scorecard
	$(UV) run python -m recon.suite

## --- quality --------------------------------------------------------------

test: ## Run service pytest + dashboard vitest
	$(UV) run pytest
	$(PNPM) test

lint: ## Lint both packages (no writes)
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(PNPM) lint

fmt: ## Auto-format both packages
	$(UV) run ruff format .
	$(UV) run ruff check --fix .
	$(PNPM) lint --fix
