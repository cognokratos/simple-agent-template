SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE ?= docker compose
EVAL_COMPOSE := $(COMPOSE) --profile evaluation
DEBUG_COMPOSE := $(COMPOSE) -f docker-compose.yml -f docker-compose.debug.yml
OPEN ?= open

OLLAMA_MODEL ?= qwen3:8b
OLLAMA_GUARD_MODEL ?= $(OLLAMA_MODEL)
WAIT_TIMEOUT ?= 300
LOG_TAIL ?= 200

SUITE ?= all
RUN_NAME ?=
FAIL_THRESHOLD ?= 1.0
ALLOW_FAILURES ?= 0
REPLACE_DATASET ?= 0

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
	help env pull-models config config-debug dev up up-build down stop restart recreate \
	debug-up debug-down ps status wait health logs logs-app logs-agent logs-ui logs-mcp \
	logs-db logs-gateway logs-keycloak logs-mlflow logs-otel logs-observability build \
	rebuild-agent rebuild-ui rebuild-mcp rebuild-gateway verify-mcp fixtures \
	verify-guardrails shell-agent shell-db open-ui open-gateway open-keycloak open-agent \
	open-mlflow open-all login-info security-config-test auth-test security-test reset-auth reset-data \
	eval-list eval-bootstrap eval-bootstrap-replace eval-bootstrap-guardrails \
	eval-bootstrap-tools eval eval-guardrails eval-tools eval-all \
	eval-all-allow-failures eval-test test

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

config: ## Render and validate the normal Docker Compose configuration
	$(COMPOSE) config

config-debug: ## Render and validate the opt-in debug port override
	$(DEBUG_COMPOSE) config

dev: env ## Rebuild and restart the complete secured development cluster
	$(COMPOSE) down --remove-orphans
	$(COMPOSE) up -d --build --remove-orphans
	@$(MAKE) --no-print-directory wait
	@$(MAKE) --no-print-directory ps
	@$(MAKE) --no-print-directory login-info

up: env ## Start the secured cluster without rebuilding images
	$(COMPOSE) up -d --remove-orphans

up-build: env ## Build changed images and start the secured cluster
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

debug-up: env ## Start with loopback-only gateway, NAT, and MCP diagnostic ports exposed
	$(DEBUG_COMPOSE) up -d --build --remove-orphans
	@$(MAKE) --no-print-directory wait

debug-down: ## Stop the cluster started with the debug override
	$(DEBUG_COMPOSE) down --remove-orphans

ps: ## Show cluster service status
	$(COMPOSE) ps

status: ps ## Alias for make ps

wait: ## Wait for UI, gateway, Keycloak, MLflow, and Collector readiness
	@deadline=$$((SECONDS + $(WAIT_TIMEOUT))); \
	printf "Waiting up to $(WAIT_TIMEOUT)s for the secured cluster"; \
	until \
		curl -fsS http://localhost:3000/ >/dev/null 2>&1 && \
		$(COMPOSE) exec -T gateway curl -fsS http://127.0.0.1:8081/ready >/dev/null 2>&1 && \
		curl -fsS http://localhost:8082/realms/$${KEYCLOAK_REALM:-alerts}/.well-known/openid-configuration >/dev/null 2>&1 && \
		curl -fsS http://localhost:5000/health >/dev/null 2>&1 && \
		curl -fsS http://localhost:13133/ >/dev/null 2>&1; \
	do \
		if (( SECONDS >= deadline )); then \
			echo; \
			echo "Timed out. Run 'make logs-app', 'make logs-keycloak', and 'make logs-observability'."; \
			exit 1; \
		fi; \
		printf "."; \
		sleep 3; \
	done; \
	echo; \
	echo "Secured cluster is ready."

health: ## Check all public endpoints and internal service health
	@curl -fsS http://localhost:3000/ >/dev/null && echo "OK  assistant-ui   http://localhost:3000"
	@$(COMPOSE) exec -T gateway curl -fsS http://127.0.0.1:8081/ready >/dev/null && echo "OK  Rust gateway   internal only"
	@curl -fsS http://localhost:8082/realms/$${KEYCLOAK_REALM:-alerts}/.well-known/openid-configuration >/dev/null && echo "OK  Keycloak       http://localhost:8082"
	@curl -fsS http://localhost:5000/health >/dev/null && echo "OK  MLflow         http://localhost:5000"
	@curl -fsS http://localhost:13133/ >/dev/null && echo "OK  OTel Collector http://localhost:13133"
	@$(COMPOSE) exec -T agent python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)" >/dev/null && echo "OK  NAT agent      internal only"
	@$(COMPOSE) exec -T mcp-server curl -fsS http://127.0.0.1:8080/health >/dev/null && echo "OK  MCP server     internal only"

logs: ## Follow logs from every normal cluster service
	$(COMPOSE) logs -f --tail=$(LOG_TAIL)

logs-app: ## Follow PostgreSQL, MCP, agent, gateway, Keycloak, and UI logs
	$(COMPOSE) logs -f --tail=$(LOG_TAIL) postgres mcp-server agent gateway keycloak ui

logs-observability: ## Follow agent, OpenTelemetry Collector, and MLflow logs
	$(COMPOSE) logs -f --tail=$(LOG_TAIL) agent otel-collector mlflow

logs-agent: ## Follow NAT agent logs
	$(COMPOSE) logs -f --tail=$(LOG_TAIL) agent

logs-ui: ## Follow assistant-ui logs
	$(COMPOSE) logs -f --tail=$(LOG_TAIL) ui

logs-mcp: ## Follow Rust MCP server logs
	$(COMPOSE) logs -f --tail=$(LOG_TAIL) mcp-server

logs-gateway: ## Follow Rust authentication gateway logs
	$(COMPOSE) logs -f --tail=$(LOG_TAIL) gateway

logs-keycloak: ## Follow Keycloak and realm-init logs
	$(COMPOSE) logs -f --tail=$(LOG_TAIL) keycloak keycloak-realm-init

logs-db: ## Follow PostgreSQL logs
	$(COMPOSE) logs -f --tail=$(LOG_TAIL) postgres

logs-mlflow: ## Follow MLflow logs
	$(COMPOSE) logs -f --tail=$(LOG_TAIL) mlflow

logs-otel: ## Follow OpenTelemetry Collector logs
	$(COMPOSE) logs -f --tail=$(LOG_TAIL) otel-collector

rebuild-agent: ## Rebuild and force-recreate the NAT agent and its dependents
	$(COMPOSE) build agent
	$(COMPOSE) up -d --force-recreate agent gateway ui

rebuild-ui: ## Rebuild and force-recreate assistant-ui
	$(COMPOSE) build ui
	$(COMPOSE) up -d --force-recreate ui

rebuild-mcp: ## Rebuild and force-recreate MCP, agent, gateway, and UI
	$(COMPOSE) build mcp-server
	$(COMPOSE) up -d --force-recreate mcp-server agent gateway ui

rebuild-gateway: ## Rebuild and force-recreate the Rust gateway and UI
	$(COMPOSE) build gateway
	$(COMPOSE) up -d --force-recreate gateway ui

verify-mcp: ## Verify that MCP rejects missing keys and accepts the agent key
	$(COMPOSE) exec -T agent python /app/verify_mcp_auth.py

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

open-gateway: ## Open the gateway health endpoint; requires `make debug-up`
	$(OPEN) http://localhost:8081/health

open-keycloak: ## Open the Keycloak admin console
	$(OPEN) http://localhost:8082/admin/

open-agent: ## Open NAT Swagger UI; requires `make debug-up`
	$(OPEN) http://localhost:8000/docs

open-mlflow: ## Open MLflow in the default browser
	$(OPEN) http://localhost:5000

open-all: ## Open assistant-ui, Keycloak admin, and MLflow
	@$(MAKE) --no-print-directory open-ui
	@$(MAKE) --no-print-directory open-keycloak
	@$(MAKE) --no-print-directory open-mlflow

login-info: ## Print the development login URLs and credentials
	@printf "UI:             http://localhost:3000\n"
	@printf "Keycloak admin: http://localhost:8082/admin/  (%s / %s)\n" "$${KEYCLOAK_ADMIN_USERNAME:-admin}" "$${KEYCLOAK_ADMIN_PASSWORD:-admin}"
	@printf "Demo analyst:   %s / %s\n" "$${KEYCLOAK_ANALYST_USERNAME:-analyst}" "$${KEYCLOAK_ANALYST_PASSWORD:-analyst}"

security-config-test: ## Validate the resolved Compose topology and security-critical source wiring
	$(COMPOSE) config --format json | python3 scripts/verify_security_config.py
	python3 scripts/verify_security_sources.py

auth-test: ## Smoke-test Keycloak discovery and every authentication boundary
	@set -euo pipefail; \
	realm="$${KEYCLOAK_REALM:-alerts}"; \
	login_cookie="$${GATEWAY_LOGIN_COOKIE:-alerts_gateway_login}"; \
	login_headers=$$(mktemp); trap 'rm -f "$$login_headers"' EXIT; \
	curl -fsS "http://localhost:8082/realms/$$realm/.well-known/openid-configuration" >/dev/null; \
	curl -sS -D "$$login_headers" -o /dev/null http://localhost:3000/api/gateway/auth/login; \
	location=$$(awk 'BEGIN{IGNORECASE=1} /^location:/ {sub(/^[^:]+:[[:space:]]*/, ""); sub(/\r$$/, ""); print; exit}' "$$login_headers"); \
	[[ "$$location" == http://localhost:8082/realms/* ]] || { echo "UI-proxied login did not redirect to Keycloak: $$location"; exit 1; }; \
	grep -Eqi "^set-cookie: $${login_cookie}=.*Path=/api/gateway/auth/callback;.*HttpOnly;.*SameSite=Lax" "$$login_headers" || { echo "UI did not relay the narrow HttpOnly login cookie"; exit 1; }; \
	python3 -c 'import sys, urllib.parse; q=urllib.parse.parse_qs(urllib.parse.urlparse(sys.argv[1]).query); assert q.get("redirect_uri") == [sys.argv[2]], q' "$$location" "$${OIDC_CALLBACK_URL:-http://localhost:3000/api/gateway/auth/callback}"; \
	session_status=$$(curl -sS -o /dev/null -w '%{http_code}' http://localhost:3000/api/gateway/auth/session); \
	[[ "$$session_status" == 200 ]] || { echo "UI-proxied anonymous session returned $$session_status, expected 200"; exit 1; }; \
	status=$$($(COMPOSE) exec -T gateway curl -sS -o /dev/null -w '%{http_code}' -X POST -H 'content-type: application/json' -d '{"messages":[]}' http://127.0.0.1:8081/api/chat); \
	[[ "$$status" == 401 ]] || { echo "Unauthenticated gateway request returned $$status, expected 401"; exit 1; }; \
	unknown_status=$$($(COMPOSE) exec -T gateway curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8081/v1/workflow/full); \
	[[ "$$unknown_status" == 404 ]] || { echo "Gateway exposed an unexpected path with status $$unknown_status"; exit 1; }; \
	agent_status=$$($(COMPOSE) exec -T gateway curl -sS -o /dev/null -w '%{http_code}' -X POST -H 'content-type: application/json' -d '{"messages":[]}' http://agent:8000/v1/workflow/full); \
	[[ "$$agent_status" == 401 ]] || { echo "Agent without API key returned $$agent_status, expected 401"; exit 1; }; \
	authenticated_agent_status=$$($(COMPOSE) exec -T gateway sh -lc 'curl -sS -o /dev/null -w "%{http_code}" -X POST -H "Authorization: Bearer $$AGENT_API_KEY" -H "content-type: application/json" -d "{\"messages\":[]}" http://agent:8000/v1/workflow/full'); \
	[[ "$$authenticated_agent_status" != 401 ]] || { echo "Agent rejected the configured API key"; exit 1; }; \
	mcp_status=$$($(COMPOSE) exec -T gateway curl -sS -o /dev/null -w '%{http_code}' http://mcp-server:8080/mcp); \
	[[ "$$mcp_status" == 401 ]] || { echo "MCP without API key returned $$mcp_status, expected 401"; exit 1; }; \
	echo "Authentication boundary smoke tests passed."

security-test: security-config-test auth-test verify-mcp ## Run all authentication and topology tests

reset-auth: ## Delete Keycloak data/import volumes and rebuild authentication services
	@read -r -p "Delete Keycloak realm and sessions? [y/N] " answer; \
	if [[ ! "$$answer" =~ ^[Yy]$$ ]]; then echo "Cancelled."; exit 1; fi
	@project="$$( $(COMPOSE) config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])' )"; \
	$(COMPOSE) down --remove-orphans; \
	for logical_name in keycloak-data keycloak-import; do \
		volumes="$$(docker volume ls -q \
			--filter "label=com.docker.compose.project=$$project" \
			--filter "label=com.docker.compose.volume=$$logical_name")"; \
		if [[ -n "$$volumes" ]]; then docker volume rm $$volumes; fi; \
	done
	$(COMPOSE) up -d --build --remove-orphans
	@$(MAKE) --no-print-directory wait

reset-data: ## Delete all persistent volumes, rebuild, and restart everything (destructive)
	@read -r -p "Delete PostgreSQL, MLflow, and Keycloak data volumes? [y/N] " answer; \
	if [[ ! "$$answer" =~ ^[Yy]$$ ]]; then echo "Cancelled."; exit 1; fi
	$(COMPOSE) down -v --remove-orphans
	$(COMPOSE) up -d --build --remove-orphans
	@$(MAKE) --no-print-directory wait

# -----------------------------------------------------------------------------
# MLflow live evaluation
# -----------------------------------------------------------------------------

eval-list: ## Show configured evaluation suites, experiments, and datasets
	$(EVAL_COMPOSE) run --rm evaluator python -m evaluation list

eval-bootstrap: ## Create or merge MLflow datasets; choose with SUITE=all|guardrails|tools
	$(EVAL_COMPOSE) run --rm evaluator python -m evaluation bootstrap --suite $(SUITE)

eval-bootstrap-replace: ## Replace MLflow datasets from JSON
	$(EVAL_COMPOSE) run --rm evaluator python -m evaluation bootstrap --suite $(SUITE) --replace

eval-bootstrap-guardrails: ## Create or merge only the Guardrails dataset
	@$(MAKE) --no-print-directory eval-bootstrap SUITE=guardrails

eval-bootstrap-tools: ## Create or merge only the tool-calling dataset
	@$(MAKE) --no-print-directory eval-bootstrap SUITE=tools

eval: ## Run a live evaluation; configure with SUITE, RUN_NAME, and other variables
	$(EVAL_COMPOSE) run --rm evaluator python -m evaluation run $(EVAL_RUN_ARGS)

eval-guardrails: ## Run only the Guardrails live evaluation suite
	@$(MAKE) --no-print-directory eval SUITE=guardrails

eval-tools: ## Run only the tool-calling live evaluation suite
	@$(MAKE) --no-print-directory eval SUITE=tools

eval-all: ## Run both live evaluation suites with regression gates enabled
	@$(MAKE) --no-print-directory eval SUITE=all

eval-all-allow-failures: ## Run both suites without a nonzero regression-gate exit
	@$(MAKE) --no-print-directory eval SUITE=all ALLOW_FAILURES=1

eval-test: ## Run evaluator parser and deterministic-scorer unit tests
	$(EVAL_COMPOSE) run --rm evaluator python -m unittest discover -s evaluation/tests -p 'test_*.py' -v

test: eval-test verify-guardrails security-test ## Run evaluator, Guardrails, and security tests
