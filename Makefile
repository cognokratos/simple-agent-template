SHELL := /bin/bash
.DEFAULT_GOAL := help

# ---------------------------------------------------------------------------
# Source identity, resolved on the host and exported into Compose.
#
# The agent image bakes GIT_COMMIT in as a build argument, so `GET /version`
# reports the commit it was *built* from; the evaluator records it alongside the
# harness commit, and a disagreement means the running container is not this
# source tree. Neither container can compute these itself — the evaluator has no
# .git and no working tree, and the agent has neither at runtime.
#
# Which source answered is recorded rather than inferred:
#
#   git      a real checkout: commit from git, dirty observed
#   unknown  no git tree — an exported tarball, a build context — so the commit
#            is honestly unknown and dirty is left *unset* rather than reported
#            as clean
#
# The critical property is that "cannot inspect git state" never renders as
# "verified clean git tree". GIT_DIRTY is empty unless a real git tree was
# examined, and the evaluator maps empty to null.
#
# `dirty` means "the inputs differ from this commit". evaluation/results is
# excluded because those files are the *output* of a run: counting them would
# make every run after the first report a dirty tree on account of the previous
# run's own artifacts, which says nothing about what was evaluated.
# ---------------------------------------------------------------------------
HAVE_GIT := $(shell git rev-parse --git-dir >/dev/null 2>&1 && echo yes)

ifeq ($(HAVE_GIT),yes)
GIT_SOURCE ?= git
GIT_COMMIT ?= $(shell git rev-parse HEAD)
GIT_DIRTY ?= $(shell test -n "$$(git status --porcelain -- . ':(exclude)evaluation/results' 2>/dev/null)" && echo true || echo false)
else
GIT_SOURCE ?= unknown
GIT_COMMIT ?= unknown
GIT_DIRTY ?=
endif

export GIT_COMMIT
export GIT_DIRTY
export GIT_SOURCE

COMPOSE ?= docker compose
EVAL_COMPOSE := $(COMPOSE) --profile evaluation
DEBUG_COMPOSE := $(COMPOSE) -f docker-compose.yml -f docker-compose.debug.yml
OPEN ?= open

# Provider-neutral: point these at any OpenAI-compatible endpoint.
LLM_MODEL ?= qwen3:8b
LLM_GUARD_MODEL ?= $(LLM_MODEL)
LLM_BASE_URL ?= http://host.docker.internal:11434/v1
POSTGRES_USER ?= tickets
POSTGRES_DB ?= tickets
# Host-side MLflow port. Overridable because 5000 collides with macOS AirPlay
# Receiver and with any other MLflow on the machine; the container port is fixed.
MLFLOW_PORT ?= 5000
UI_PORT ?= 3000
KEYCLOAK_PORT ?= 8082
# Browser-facing URLs. Derived from the ports above so that moving a host port
# does not silently break the checks that probe it; override either directly if
# the stack sits behind a proxy.
UI_PUBLIC_URL ?= http://localhost:$(UI_PORT)
KEYCLOAK_PUBLIC_URL ?= http://localhost:$(KEYCLOAK_PORT)
OIDC_CALLBACK_URL ?= $(UI_PUBLIC_URL)/api/gateway/auth/callback
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
	print-provenance version static-check logs-inspector network-test eval-test-host \
	verify-approvals verify-approvals-rust verify-llm-config \
	verify-input-guardrails verify-output-guardrails verify-rails verify-guardrails \
	verify-stream-adapter verify-trace-pipeline trace-test traces \
	inspector open-inspector inspector-tools network-test \
	shell-agent shell-db open-ui open-gateway open-keycloak open-agent \
	open-mlflow open-all login-info security-config-test auth-test security-test reset-auth reset-data \
	eval-list eval-bootstrap eval-bootstrap-replace eval-bootstrap-guardrails \
	eval-bootstrap-tools eval-bootstrap-grounding eval-bootstrap-injection \
	eval eval-guardrails eval-tools eval-grounding eval-injection eval-all \
	eval-all-allow-failures eval-test test

help: ## Show all available targets
	@printf "Usage: make <target> [VARIABLE=value]\n\n"
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-28s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@printf "\nEvaluation variables:\n"
	@printf "  SUITE=all|guardrails|tools|grounding|injection   RUN_NAME=<name>   FAIL_THRESHOLD=1.0\n"
	@printf "  ALLOW_FAILURES=0|1           REPLACE_DATASET=0|1\n"

env: ## Create .env from .env.example when it does not exist
	@if [[ -f .env ]]; then \
		echo ".env already exists"; \
	else \
		cp .env.example .env; \
		echo "Created .env from .env.example"; \
	fi

pull-models: ## Pull the configured agent and guard models (no-op unless LLM_BASE_URL is Ollama)
	@if [[ "$(LLM_BASE_URL)" != *"11434"* ]]; then \
		echo "LLM_BASE_URL is not an Ollama endpoint ($(LLM_BASE_URL)); nothing to pull."; \
		exit 0; \
	fi; \
	if ! command -v ollama >/dev/null 2>&1; then \
		echo "ollama is not installed; skipping. Point LLM_BASE_URL at a hosted endpoint instead."; \
		exit 0; \
	fi; \
	ollama pull "$(LLM_MODEL)"; \
	if [[ "$(LLM_GUARD_MODEL)" != "$(LLM_MODEL)" ]]; then ollama pull "$(LLM_GUARD_MODEL)"; fi

print-provenance: ## Print the resolved source identity
	@echo "GIT_SOURCE=$(GIT_SOURCE)"
	@echo "GIT_COMMIT=$(GIT_COMMIT)"
	@echo "GIT_DIRTY=$(GIT_DIRTY)"

version: ## Print the running agent's own provenance from its authenticated /version
	@$(COMPOSE) exec -T agent python -c "import json,os,urllib.request; \
	req=urllib.request.Request('http://127.0.0.1:8000/version', \
	headers={'Authorization':'Bearer '+os.environ['NAT_GATEWAY_API_KEY']}); \
	print(json.dumps(json.load(urllib.request.urlopen(req, timeout=10)), indent=2))"

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
		curl -fsS http://localhost:$(UI_PORT)/ >/dev/null 2>&1 && \
		$(COMPOSE) exec -T gateway curl -fsS http://127.0.0.1:8081/ready >/dev/null 2>&1 && \
		curl -fsS http://localhost:$(KEYCLOAK_PORT)/realms/$${KEYCLOAK_REALM:-tickets}/.well-known/openid-configuration >/dev/null 2>&1 && \
		curl -fsS http://localhost:$(MLFLOW_PORT)/health >/dev/null 2>&1 && \
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
	@curl -fsS http://localhost:$(UI_PORT)/ >/dev/null && echo "OK  assistant-ui   http://localhost:$(UI_PORT)"
	@$(COMPOSE) exec -T gateway curl -fsS http://127.0.0.1:8081/ready >/dev/null && echo "OK  Rust gateway   internal only"
	@curl -fsS http://localhost:$(KEYCLOAK_PORT)/realms/$${KEYCLOAK_REALM:-tickets}/.well-known/openid-configuration >/dev/null && echo "OK  Keycloak       http://localhost:$(KEYCLOAK_PORT)"
	@curl -fsS http://localhost:$(MLFLOW_PORT)/health >/dev/null && echo "OK  MLflow         http://localhost:$(MLFLOW_PORT)"
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

verify-input-guardrails: ## Run the input-guardrail regression smoke test
	$(COMPOSE) exec agent python /app/verify_input_guardrails.py

verify-stream-adapter: ## Verify the NAT SSE adapter preserves numeric/scalar answer chunks
	cd ui && node --experimental-strip-types scripts/verify-nat-wire.mjs

verify-output-guardrails: ## Verify streamed output release, secret blocking, and PII masking (loads a ~600MB spaCy model)
	$(COMPOSE) exec -T agent python /app/verify_output_guardrails.py

verify-rails: ## Run the live NeMo Guardrails input/output rail regression suite
	$(COMPOSE) exec -T agent python /app/verify_guardrails_rails.py

verify-guardrails: verify-input-guardrails verify-output-guardrails verify-rails ## Run every guardrail regression check

verify-llm-config: ## Verify the LLM provider builds and omits empty optional parameters
	$(COMPOSE) exec -T agent python /app/verify_llm_config.py

verify-approvals: ## Run the offline approval-boundary tests (token binding, replay, ownership)
	$(COMPOSE) exec -T agent python /app/verify_approval_tokens.py

verify-approvals-rust: ## Run the MCP approval verifier and mutation-policy tests
	cd mcp-server && cargo test approval:: && cargo test mutation::

verify-trace-pipeline: ## Run the offline observability pipeline regression tests
	$(COMPOSE) exec -T agent python /app/verify_trace_pipeline.py

trace-test: verify-trace-pipeline ## Verify observability end to end against MLflow (needs the cluster and a model)
	python3 scripts/verify_traces_e2e.py

traces: ## Print the span tree of the most recent MLflow traces
	python3 scripts/inspect_mlflow_traces.py --limit $${LIMIT:-3}

static-check: verify-stream-adapter eval-test-host security-config-test ## Offline checks needing no Docker, cluster or model (python3 + node only)
	@echo "Static checks passed."

inspector: ## Start the optional loopback-only MCP Inspector (development profile)
	$(COMPOSE) --profile dev up -d --build mcp-inspector

logs-inspector: ## Follow MCP Inspector logs
	$(COMPOSE) --profile dev logs -f --tail $(LOG_TAIL) mcp-inspector

open-inspector: ## Open MCP Inspector (token is read from .env/default)
	@token="$${MCP_INSPECTOR_API_TOKEN:-dev-mcp-inspector-token-change-me}"; \
	$(OPEN) "http://localhost:6274/?MCP_INSPECTOR_API_TOKEN=$$token"

inspector-tools: ## List MCP tools directly through Inspector CLI, bypassing NAT/gateway/UI
	$(COMPOSE) --profile dev exec -T mcp-inspector sh -lc 'mcp-inspector --cli http://mcp-server:8080/mcp --transport http --header "Authorization: Bearer $$MCP_API_KEY" --method tools/list'

shell-agent: ## Open a shell inside the NAT agent container
	$(COMPOSE) exec agent bash

shell-db: ## Open psql in the PostgreSQL container
	$(COMPOSE) exec postgres sh -lc 'psql -U "$$POSTGRES_USER" -d "$$POSTGRES_DB"'

open-ui: ## Open assistant-ui in the default browser
	$(OPEN) $(UI_PUBLIC_URL)

open-gateway: ## Open the gateway health endpoint; requires `make debug-up`
	$(OPEN) http://localhost:8081/health

open-keycloak: ## Open the Keycloak admin console
	$(OPEN) $(KEYCLOAK_PUBLIC_URL)/admin/

open-agent: ## Open NAT Swagger UI; requires `make debug-up`
	$(OPEN) http://localhost:8000/docs

open-mlflow: ## Open MLflow in the default browser
	$(OPEN) http://localhost:$(MLFLOW_PORT)

open-all: ## Open assistant-ui, Keycloak admin, and MLflow
	@$(MAKE) --no-print-directory open-ui
	@$(MAKE) --no-print-directory open-keycloak
	@$(MAKE) --no-print-directory open-mlflow

login-info: ## Print the development login URLs and credentials
	@printf "UI:             http://localhost:$(UI_PORT)\n"
	@printf "Keycloak admin: http://localhost:$(KEYCLOAK_PORT)/admin/  (%s / %s)\n" "$${KEYCLOAK_ADMIN_USERNAME:-admin}" "$${KEYCLOAK_ADMIN_PASSWORD:-admin}"
	@printf "Demo agent:     %s / %s\n" "$${KEYCLOAK_AGENT_USERNAME:-agent}" "$${KEYCLOAK_AGENT_PASSWORD:-agent}"

# Every profile is enabled for the render. `docker compose config` omits
# profile-gated services by default, so without this the check silently skipped
# the evaluator and failed on "missing service 'evaluator'" — which is exactly
# what it did on every invocation before this was fixed.
security-config-test: ## Validate the resolved Compose topology and security-critical source wiring
	$(COMPOSE) --profile evaluation --profile dev config --format json | python3 scripts/verify_security_config.py
	python3 scripts/verify_security_sources.py

# The MCP key boundary is probed from the *agent*, not the gateway: the gateway
# deliberately does not share mcp_net, so from there the host does not even
# resolve. That stronger property is asserted first.
auth-test: ## Smoke-test Keycloak discovery and every authentication boundary
	@set -euo pipefail; \
	realm="$${KEYCLOAK_REALM:-tickets}"; \
	login_cookie="$${GATEWAY_LOGIN_COOKIE:-tickets_gateway_login}"; \
	login_headers=$$(mktemp); trap 'rm -f "$$login_headers"' EXIT; \
	curl -fsS "$(KEYCLOAK_PUBLIC_URL)/realms/$$realm/.well-known/openid-configuration" >/dev/null; \
	curl -sS -D "$$login_headers" -o /dev/null $(UI_PUBLIC_URL)/api/gateway/auth/login; \
	location=$$(awk 'BEGIN{IGNORECASE=1} /^location:/ {sub(/^[^:]+:[[:space:]]*/, ""); sub(/\r$$/, ""); print; exit}' "$$login_headers"); \
	[[ "$$location" == $(KEYCLOAK_PUBLIC_URL)/realms/* ]] || { echo "UI-proxied login did not redirect to Keycloak: $$location"; exit 1; }; \
	grep -Eqi "^set-cookie: $${login_cookie}=.*Path=/api/gateway/auth/callback;.*HttpOnly;.*SameSite=Lax" "$$login_headers" || { echo "UI did not relay the narrow HttpOnly login cookie"; exit 1; }; \
	python3 -c 'import sys, urllib.parse; q=urllib.parse.parse_qs(urllib.parse.urlparse(sys.argv[1]).query); assert q.get("redirect_uri") == [sys.argv[2]], q' "$$location" "$(OIDC_CALLBACK_URL)"; \
	session_status=$$(curl -sS -o /dev/null -w '%{http_code}' $(UI_PUBLIC_URL)/api/gateway/auth/session); \
	[[ "$$session_status" == 200 ]] || { echo "UI-proxied anonymous session returned $$session_status, expected 200"; exit 1; }; \
	status=$$($(COMPOSE) exec -T gateway curl -sS -o /dev/null -w '%{http_code}' -X POST -H 'content-type: application/json' -d '{"messages":[]}' http://127.0.0.1:8081/api/chat); \
	[[ "$$status" == 401 ]] || { echo "Unauthenticated gateway request returned $$status, expected 401"; exit 1; }; \
	unknown_status=$$($(COMPOSE) exec -T gateway curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8081/v1/workflow/full); \
	[[ "$$unknown_status" == 404 ]] || { echo "Gateway exposed an unexpected path with status $$unknown_status"; exit 1; }; \
	agent_status=$$($(COMPOSE) exec -T gateway curl -sS -o /dev/null -w '%{http_code}' -X POST -H 'content-type: application/json' -d '{"messages":[]}' http://agent:8000/v1/workflow/full); \
	[[ "$$agent_status" == 401 ]] || { echo "Agent without API key returned $$agent_status, expected 401"; exit 1; }; \
	authenticated_agent_status=$$($(COMPOSE) exec -T gateway sh -lc 'curl -sS -o /dev/null -w "%{http_code}" -X POST -H "Authorization: Bearer $$AGENT_API_KEY" -H "content-type: application/json" -d "{\"messages\":[]}" http://agent:8000/v1/workflow/full'); \
	[[ "$$authenticated_agent_status" != 401 ]] || { echo "Agent rejected the configured API key"; exit 1; }; \
	if $(COMPOSE) exec -T gateway curl -sS -o /dev/null --max-time 5 http://mcp-server:8080/health 2>/dev/null; then \
		echo "gateway can reach MCP; it must not share mcp_net"; exit 1; \
	fi; \
	mcp_status=$$($(COMPOSE) exec -T agent sh -lc 'python -c "import urllib.request,urllib.error; \
req=urllib.request.Request(\"http://mcp-server:8080/mcp\", method=\"POST\"); \
print(urllib.request.urlopen(req, timeout=5).status)" 2>&1 | grep -oE "HTTP Error [0-9]+" | grep -oE "[0-9]+" || echo 000'); \
	[[ "$$mcp_status" == 401 ]] || { echo "MCP without API key returned $$mcp_status, expected 401"; exit 1; }; \
	echo "Authentication boundary smoke tests passed."

network-test: ## Assert the east-west topology at runtime: who can reach NAT, MCP and the database
	@set -euo pipefail; \
	echo "host -> gateway/NAT/MCP/PostgreSQL must all be unreachable (no published ports)"; \
	published="$$($(COMPOSE) ps --format json 2>/dev/null | python3 -c 'import json,sys; \
print(" ".join(str(p["PublishedPort"]) for line in sys.stdin if line.strip() \
for p in (json.loads(line).get("Publishers") or []) if p.get("PublishedPort")))')"; \
	echo "  this project publishes host ports: $${published:-none}"; \
	for port in 8081 8000 8080 5432; do \
		for owned in $$published; do \
			if [[ "$$owned" == "$$port" ]]; then \
				echo "port $$port is published by this project; it must not be"; exit 1; \
			fi; \
		done; \
		if curl -sS --max-time 3 -o /dev/null "http://127.0.0.1:$$port/" 2>/dev/null; then \
			echo "  note: something else on this host listens on $$port. It is not one of ours"; \
			echo "        — this project publishes no host port for it — so the reachability"; \
			echo "        probe cannot be conclusive. Free the port for an unambiguous result."; \
		fi; \
	done; \
	echo "  ok: gateway, NAT, MCP and PostgreSQL are not published by this project"; \
	$(COMPOSE) exec -T gateway sh -lc 'curl -sS --max-time 5 -o /dev/null http://agent:8000/health' \
		|| { echo "gateway cannot reach NAT"; exit 1; }; \
	echo "  ok: gateway -> NAT"; \
	$(COMPOSE) exec -T agent sh -lc 'python -c "import urllib.request; urllib.request.urlopen(\"http://mcp-server:8080/health\", timeout=5)"' \
		|| { echo "NAT cannot reach MCP"; exit 1; }; \
	echo "  ok: NAT -> MCP"; \
	if $(COMPOSE) exec -T gateway sh -lc 'curl -sS --max-time 5 -o /dev/null http://mcp-server:8080/health' 2>/dev/null; then \
		echo "gateway can reach MCP; it must not share mcp_net"; exit 1; \
	fi; \
	echo "  ok: gateway cannot reach MCP"; \
	if $(COMPOSE) exec -T ui sh -lc 'wget -q -T 5 -O /dev/null http://agent:8000/health' 2>/dev/null; then \
		echo "assistant-ui can reach NAT; it must not share agent_net"; exit 1; \
	fi; \
	echo "  ok: assistant-ui cannot reach NAT"; \
	if $(COMPOSE) exec -T ui sh -lc 'wget -q -T 5 -O /dev/null http://mcp-server:8080/health' 2>/dev/null; then \
		echo "assistant-ui can reach MCP; it must not share mcp_net"; exit 1; \
	fi; \
	echo "  ok: assistant-ui cannot reach MCP"; \
	if $(COMPOSE) exec -T agent sh -lc 'python -c "import socket; socket.create_connection((\"postgres\", 5432), 5)"' 2>/dev/null; then \
		echo "NAT can reach PostgreSQL directly; only the MCP server may"; exit 1; \
	fi; \
	echo "  ok: NAT cannot reach PostgreSQL directly"; \
	echo "Network isolation tests passed."

security-test: security-config-test network-test auth-test verify-mcp ## Run all authentication and topology tests

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

eval-bootstrap: ## Create or merge MLflow datasets; choose with SUITE=all|guardrails|tools|grounding|injection
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

eval-grounding: ## Run only the grounded-answer live evaluation suite
	$(EVAL_COMPOSE) run --rm evaluator python -m evaluation run --suite grounding --fail-threshold $(FAIL_THRESHOLD)

eval-injection: ## Run only the data-plane prompt-injection suite (reads seeded TKT-INJ-* fixtures; mutates nothing)
	$(EVAL_COMPOSE) run --rm evaluator python -m evaluation run --suite injection --fail-threshold $(FAIL_THRESHOLD)

eval-bootstrap-grounding: ## Create or merge only the grounding dataset
	$(EVAL_COMPOSE) run --rm evaluator python -m evaluation bootstrap --suite grounding

eval-bootstrap-injection: ## Create or merge only the injection dataset
	$(EVAL_COMPOSE) run --rm evaluator python -m evaluation bootstrap --suite injection

eval-tools: ## Run only the tool-calling live evaluation suite
	@$(MAKE) --no-print-directory eval SUITE=tools

eval-all: ## Run all live evaluation suites with regression gates enabled
	@$(MAKE) --no-print-directory eval SUITE=all

eval-all-allow-failures: ## Run all suites without a nonzero regression-gate exit
	@$(MAKE) --no-print-directory eval SUITE=all ALLOW_FAILURES=1

# --no-deps: these tests stub MLflow and talk to a local HTTP fixture, so they
# need the image's Python but not the cluster. Without it, `docker compose run`
# starts PostgreSQL, MCP, MLflow and the agent just to run unit tests.
eval-test: ## Run evaluator parser and scorer unit tests in the evaluator image
	$(EVAL_COMPOSE) run --rm --no-deps evaluator python -m unittest discover -s evaluation/tests -t . -p 'test_*.py' -v

eval-test-host: ## Run the same evaluator unit tests on the host (no Docker)
	python3 -m unittest discover -s evaluation/tests -t . -p 'test_*.py' -v

test: eval-test verify-llm-config verify-guardrails verify-trace-pipeline verify-approvals security-test ## Run evaluator, Guardrails, observability, approval-boundary, and security tests
