# Cairn. `make help` lists what matters.

SHELL := /bin/bash
PY := uv run
export CAIRN_DB_DSN ?= postgresql+asyncpg://cairn:cairn@localhost:55432/cairn
export CAIRN_TEST_DB_DSN ?= $(CAIRN_DB_DSN)
export CAIRN_S3_BUCKET ?= :memory:
export CAIRN_POLICY_ENABLED ?= false
export CAIRN_PROMPT_DIR ?= cairn-deploy/prompts

.DEFAULT_GOAL := help

.PHONY: help
help: ## List targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install: ## Sync the workspace and the UI
	uv sync --all-packages --dev
	cd ui && npm ci --no-audit --no-fund

.PHONY: up
up: ## Start Postgres, Redis, MinIO and OPA
	docker compose up -d
	@until docker compose exec -T postgres pg_isready -U cairn >/dev/null 2>&1; do sleep 1; done
	@echo "dependencies ready"

.PHONY: down
down: ## Stop them
	docker compose down

.PHONY: migrate
migrate: ## Apply database migrations
	cd packages/cairn-core && $(PY) alembic upgrade head

.PHONY: revision
revision: ## Autogenerate a migration: make revision m="add x"
	cd packages/cairn-core && $(PY) alembic revision --autogenerate -m "$(m)"

.PHONY: test
test: ## Run the test suite (needs `make up`)
	$(PY) pytest -q

.PHONY: selfcheck
selfcheck: ## Run every module's built-in check
	@for m in cairn_core.sensitivity cairn_core.auth cairn_core.tokens \
	          cairn_core.artifacts cairn_core.prompts cairn_router.routing \
	          cairn_router.configmaps cairn_router.providers; do \
	  $(PY) python -m $$m || exit 1; \
	done
	@$(PY) python -m cairn_mcp_runbooks.ingest --self-check
	@$(PY) python -m cairn_eval.gate --self-check

.PHONY: lint
lint: ## ruff + mypy + UI typecheck
	$(PY) ruff check .
	$(PY) ruff format --check .
	$(PY) mypy packages services
	cd ui && npm run typecheck

.PHONY: fmt
fmt: ## Format everything
	$(PY) ruff format .
	$(PY) ruff check --fix .
	terraform fmt -recursive cairn-infra

.PHONY: eval
eval: ## Run the 30-scenario suite (heuristic mode; no model needed)
	$(PY) cairn-eval --mode heuristic --out eval-results.json

.PHONY: scenarios
scenarios: ## Regenerate the scenario corpus from the templates
	$(PY) python services/cairn-eval/tools/generate_scenarios.py

.PHONY: eval-stack
eval-stack: ## Bring up seeded Prometheus/Loki/deploys and load the corpus
	docker compose -f services/cairn-eval/stack/docker-compose.yml up -d
	$(PY) python services/cairn-eval/stack/seed/seed.py

.PHONY: eval-calibrate
eval-calibrate: ## Score the LLM cause judge against human labels (needs labels.jsonl)
	$(PY) python -m cairn_eval.llm_judge fixtures/judge-labels.jsonl

.PHONY: eval-record
eval-record: ## Record fixtures against a live router (costs money)
	$(PY) cairn-eval --mode record --count 30 --out eval-results.json

.PHONY: eval-gate
eval-gate: ## Compare the last run against the committed baseline
	$(PY) cairn-eval-gate --current eval-results.json --baseline fixtures/eval-baseline.json

.PHONY: policy
policy: ## Check and test the OPA bundle
	opa check cairn-deploy/policy
	opa test cairn-deploy/policy -v

.PHONY: manifests
manifests: ## Render and validate the Helm chart for every environment
	@for env in dev staging prod; do \
	  helm lint cairn-deploy/charts/cairn -f cairn-deploy/values/$$env.yaml && \
	  helm template cairn cairn-deploy/charts/cairn -f cairn-deploy/values/$$env.yaml >/dev/null || exit 1; \
	done
	@echo "manifests render for dev, staging and prod"

.PHONY: run-gateway run-orchestrator run-router
run-gateway: ## Run the gateway locally
	CAIRN_AUTH_DEV_MODE=true $(PY) cairn-gateway

run-orchestrator: ## Run the orchestrator locally
	$(PY) cairn-orchestrator

run-router: ## Run the router locally
	$(PY) cairn-router

.PHONY: mcp-stdio
mcp-stdio: ## Run the observability tools over stdio, for MCP clients
	CAIRN_MCP_STDIO=1 $(PY) cairn-mcp-observability

.PHONY: ask
ask: ## Ask from the terminal: make ask q="why did checkout spike?"
	CAIRN_DEV_USER=$(USER) $(PY) cairn ask "$(q)"

.PHONY: ci
ci: lint test selfcheck eval ## What CI runs, locally
