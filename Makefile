SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE ?= docker compose
EVAL_COMPOSE := $(COMPOSE) --profile evaluation
OPEN ?= open

OLLAMA_MODEL ?= qwen3:8b
OLLAMA_GUARD_MODEL ?= $(OLLAMA_MODEL)
WAIT_TIMEOUT ?= 240
LOG_TAIL ?= 200

# Generic evaluation controls. Override them on the command line, for example:
# make eval SUITE=guardrails RUN_NAME=guardrails-v2 FAIL_THRESHOLD=0.95
SUITE ?= all
RUN_NAME ?=
FAIL_THRESHOLD ?= 1.0
ALLOW_FAILURES ?= 0
REPLACE_DATASET ?= 0

# Import local overrides when the file exists. Docker Compose also reads it.
-include .env

EVAL_RUN_ARGS := --suite $(SUITE) --fail-threshold $(FAIL_THRESHOLD)
ifneq ($(strip $(RUN_NAME)),)
EVAL_RUN_ARGS += --run-name "$(RUN_NAME)"
endif
ifeq ($(ALLOW_FAILURES),1)
EVAL_RUN_ARGS += --allow-failures
endif
ifeq ($(REPLACE_DATASET),1)
EVAL_RUN_ARGS += --replace-dataset
endif

.PHONY: \
	help env pull-models config dev up up-build down stop restart recreate \
	ps status wait health logs logs-app logs-agent logs-ui logs-mcp logs-db \
	logs-mlflow logs-otel logs-observability build rebuild-agent rebuild-ui \
	rebuild-mcp verify-mcp fixtures verify-guardrails shell-agent shell-db \
	open-ui open-agent open-mlflow open-all reset-data \
	eval-list eval-bootstrap eval-bootstrap-replace eval-bootstrap-guardrails \
	eval-bootstrap-tools eval eval-guardrails eval-tools eval-all eval-all-allow-failures eval-test test

help: ## Show all available targets
	@printf "Usage: make <target> [VARIABLE=value]\n\n"
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-28s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@printf "\nEvaluation variables:\n"
	@printf "  SUITE=all|guardrails|tools   RUN_NAME=<name>   FAIL_THRESHOLD=1.0\n"
	@printf "  ALLOW_FAILURES=0|1           REPLACE_DATASET=0|1\n"

env: ## Create .env from .env.example when it does not exist
	@if [[ -f .env ]]; then \
		echo ".env already exists"; \
	else \
		cp .env.example .env; \
		echo "Created .env from .env.example"; \
	fi

pull-models: ## Pull the configured Ollama agent and guard models
	ollama pull "$(OLLAMA_MODEL)"
	@if [[ "$(OLLAMA_GUARD_MODEL)" != "$(OLLAMA_MODEL)" ]]; then \
		ollama pull "$(OLLAMA_GUARD_MODEL)"; \
	fi

config: ## Render and validate the Docker Compose configuration
	$(COMPOSE) config

dev: env ## Rebuild and restart the complete development cluster
	$(COMPOSE) down --remove-orphans
	$(COMPOSE) up -d --build --remove-orphans
	@$(MAKE) --no-print-directory wait
	@$(MAKE) --no-print-directory ps

up: env ## Start the cluster without rebuilding images
	$(COMPOSE) up -d --remove-orphans

up-build: env ## Build changed images and start the cluster
	$(COMPOSE) up -d --build --remove-orphans

build: ## Build all locally built service images
	$(COMPOSE) build

down: ## Stop and remove the cluster, preserving volumes
	$(COMPOSE) down --remove-orphans

stop: ## Stop services without removing containers
	$(COMPOSE) stop

restart: ## Restart existing service containers without rebuilding
	$(COMPOSE) restart

recreate: ## Force-recreate all running services without rebuilding
	$(COMPOSE) up -d --force-recreate --remove-orphans

ps: ## Show cluster service status
	$(COMPOSE) ps

status: ps ## Alias for make ps

wait: ## Wait until UI, agent, MCP, MLflow, and Collector endpoints are ready
	@deadline=$$((SECONDS + $(WAIT_TIMEOUT))); \
	printf "Waiting up to $(WAIT_TIMEOUT)s for the cluster"; \
	until \
		curl -fsS http://localhost:3000/ >/dev/null 2>&1 && \
		curl -fsS http://localhost:8000/docs >/dev/null 2>&1 && \
		curl -fsS http://localhost:8080/health >/dev/null 2>&1 && \
		curl -fsS http://localhost:5000/health >/dev/null 2>&1 && \
		curl -fsS http://localhost:13133/ >/dev/null 2>&1; \
	do \
		if (( SECONDS >= deadline )); then \
			echo; \
			echo "Timed out waiting for the cluster. Run 'make logs-app' and 'make logs-observability'."; \
			exit 1; \
		fi; \
		printf "."; \
		sleep 3; \
	done; \
	echo; \
	echo "Cluster is ready."

health: ## Check every public service endpoint
	@curl -fsS http://localhost:3000/ >/dev/null && echo "OK  assistant-ui   http://localhost:3000"
	@curl -fsS http://localhost:8000/docs >/dev/null && echo "OK  NAT agent      http://localhost:8000/docs"
	@curl -fsS http://localhost:8080/health >/dev/null && echo "OK  MCP server     http://localhost:8080/health"
	@curl -fsS http://localhost:5000/health >/dev/null && echo "OK  MLflow         http://localhost:5000"
	@curl -fsS http://localhost:13133/ >/dev/null && echo "OK  OTel Collector http://localhost:13133"

logs: ## Follow logs from every normal cluster service
	$(COMPOSE) logs -f --tail=$(LOG_TAIL)

logs-app: ## Follow PostgreSQL, MCP, agent, and UI logs
	$(COMPOSE) logs -f --tail=$(LOG_TAIL) postgres mcp-server agent ui

logs-observability: ## Follow agent, OpenTelemetry Collector, and MLflow logs
	$(COMPOSE) logs -f --tail=$(LOG_TAIL) agent otel-collector mlflow

logs-agent: ## Follow NAT agent logs
	$(COMPOSE) logs -f --tail=$(LOG_TAIL) agent

logs-ui: ## Follow assistant-ui logs
	$(COMPOSE) logs -f --tail=$(LOG_TAIL) ui

logs-mcp: ## Follow Rust MCP server logs
	$(COMPOSE) logs -f --tail=$(LOG_TAIL) mcp-server

logs-db: ## Follow PostgreSQL logs
	$(COMPOSE) logs -f --tail=$(LOG_TAIL) postgres

logs-mlflow: ## Follow MLflow logs
	$(COMPOSE) logs -f --tail=$(LOG_TAIL) mlflow

logs-otel: ## Follow OpenTelemetry Collector logs
	$(COMPOSE) logs -f --tail=$(LOG_TAIL) otel-collector

rebuild-agent: ## Rebuild and force-recreate the NAT agent
	$(COMPOSE) build agent
	$(COMPOSE) up -d --force-recreate agent

rebuild-ui: ## Rebuild and force-recreate assistant-ui
	$(COMPOSE) build ui
	$(COMPOSE) up -d --force-recreate ui

rebuild-mcp: ## Rebuild and force-recreate the Rust MCP server
	$(COMPOSE) build mcp-server
	$(COMPOSE) up -d --force-recreate mcp-server

verify-mcp: ## List MCP tools through the NAT MCP client
	$(COMPOSE) exec agent \
		nat mcp client tool list \
		--url http://mcp-server:8080/mcp

fixtures: ## Apply the synthetic Guardrails test fixtures to the current database
	$(COMPOSE) exec -T postgres \
		sh -lc 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"' \
		< db/guardrail_test_fixtures.sql

verify-guardrails: ## Run the offline input-guardrail regression smoke test
	$(COMPOSE) exec agent python /app/verify_input_guardrails.py

shell-agent: ## Open a shell inside the NAT agent container
	$(COMPOSE) exec agent bash

shell-db: ## Open psql in the PostgreSQL container
	$(COMPOSE) exec postgres sh -lc 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

open-ui: ## Open assistant-ui in the default browser
	$(OPEN) http://localhost:3000

open-agent: ## Open NAT Swagger UI in the default browser
	$(OPEN) http://localhost:8000/docs

open-mlflow: ## Open MLflow in the default browser
	$(OPEN) http://localhost:5000

open-all: ## Open assistant-ui, NAT Swagger UI, and MLflow
	@$(MAKE) --no-print-directory open-ui
	@$(MAKE) --no-print-directory open-agent
	@$(MAKE) --no-print-directory open-mlflow

reset-data: ## Delete all persistent volumes, rebuild, and restart everything (destructive)
	@read -r -p "Delete PostgreSQL and MLflow data volumes? [y/N] " answer; \
	if [[ ! "$$answer" =~ ^[Yy]$$ ]]; then \
		echo "Cancelled."; \
		exit 1; \
	fi
	$(COMPOSE) down -v --remove-orphans
	$(COMPOSE) up -d --build --remove-orphans
	@$(MAKE) --no-print-directory wait

# -----------------------------------------------------------------------------
# MLflow live evaluation
# -----------------------------------------------------------------------------

eval-list: ## Show configured evaluation suites, experiments, and datasets
	$(EVAL_COMPOSE) run --rm evaluator python -m evaluation list

eval-bootstrap: ## Create or merge MLflow datasets; choose with SUITE=all|guardrails|tools
	$(EVAL_COMPOSE) run --rm evaluator \
		python -m evaluation bootstrap --suite $(SUITE)

eval-bootstrap-replace: ## Replace MLflow datasets from JSON; choose with SUITE=all|guardrails|tools
	$(EVAL_COMPOSE) run --rm evaluator \
		python -m evaluation bootstrap --suite $(SUITE) --replace

eval-bootstrap-guardrails: ## Create or merge only the Guardrails evaluation dataset
	@$(MAKE) --no-print-directory eval-bootstrap SUITE=guardrails

eval-bootstrap-tools: ## Create or merge only the tool-calling evaluation dataset
	@$(MAKE) --no-print-directory eval-bootstrap SUITE=tools

eval: ## Run a live evaluation; configure with SUITE, RUN_NAME, and other variables
	$(EVAL_COMPOSE) run --rm evaluator \
		python -m evaluation run $(EVAL_RUN_ARGS)

eval-guardrails: ## Run only the Guardrails live evaluation suite
	@$(MAKE) --no-print-directory eval SUITE=guardrails

eval-tools: ## Run only the tool-calling live evaluation suite
	@$(MAKE) --no-print-directory eval SUITE=tools

eval-all: ## Run both live evaluation suites with regression gates enabled
	@$(MAKE) --no-print-directory eval SUITE=all

eval-all-allow-failures: ## Run both suites without a nonzero regression-gate exit
	@$(MAKE) --no-print-directory eval SUITE=all ALLOW_FAILURES=1

eval-test: ## Run evaluator parser and deterministic-scorer unit tests
	$(EVAL_COMPOSE) run --rm evaluator \
		python -m unittest discover -s evaluation/tests -p 'test_*.py' -v

test: eval-test verify-guardrails ## Run evaluator unit tests and Guardrails smoke tests
