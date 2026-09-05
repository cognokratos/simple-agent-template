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

LLM_MODEL ?= qwen3:8b
LLM_GUARD_MODEL ?= $(LLM_MODEL)
LLM_BASE_URL ?= http://host.docker.internal:11434/v1
POSTGRES_USER ?= etf_research
POSTGRES_DB ?= etf_research
# Host-side MLflow port. Overridable because 5000 collides with macOS AirPlay
# Receiver and with any other MLflow on the machine; the container port is fixed.
MLFLOW_PORT ?= 5000

# ETFs reserved for the approval-boundary suite. Disjoint from the injection
# payloads and from the demo guide (docs/DEMO.md), so `make verify-approvals` never
# disturbs a demo.
#
# The last two are the decision-authority cases, which need a fund the engine
# shortlists. Both also appear in a read-only labelled case; that case asserts the
# deterministic decision and score, which are a pure function of the ETF facts, the
# rules and the profile and never of workflow state, so committing here cannot
# affect it.
APPROVAL_TEST_ETFS := 'VJPN-LSE','IH2O-LSE','XNIF-XETRA','QQQ-NASDAQ','VHYL-LSE','EQQQ-LSE','CW8-EPA','VFEM-LSE','VWRL-LSE','IUSN-XETRA'
# Reserved for the end-to-end human-override check. Deterministic decision is
# `research`, so choosing `shortlist` is a promotion — the direction the model may
# never take on its own. Referenced by no evaluation dataset and no injection
# payload, so a HITL run and an eval run cannot disturb each other.
HITL_TEST_ETF := 'ESPO-XETRA'

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
	debug-up debug-down ps status wait health logs logs-app logs-agent logs-ui logs-mcp logs-inspector \
	logs-db logs-gateway logs-keycloak logs-mlflow logs-otel logs-observability build \
	rebuild-agent rebuild-ui rebuild-mcp rebuild-gateway verify-mcp etf-check static-check \
	print-provenance rules-test verify-approvals verify-hitl verify-hitl-audit eval-injection diagrams \
	verify-input-guardrails verify-output-guardrails verify-rails verify-guardrails \
	verify-stream-adapter verify-trace-pipeline trace-test traces shell-agent shell-db \
	open-ui open-gateway open-keycloak open-agent open-mlflow open-inspector inspector-tools \
	open-all login-info security-config-test network-test auth-test security-test reset-auth reset-data \
	eval-list eval-bootstrap eval-bootstrap-replace \
	eval-bootstrap-etf eval-bootstrap-policy eval-bootstrap-grounding eval-bootstrap-guardrails \
	eval eval-etf eval-policy eval-grounding eval-guardrails eval-all eval-suite-all \
	eval-all-allow-failures eval-test test

print-provenance: ## Print the resolved source identity
	@echo "GIT_SOURCE=$(GIT_SOURCE)"
	@echo "GIT_COMMIT=$(GIT_COMMIT)"
	@echo "GIT_DIRTY=$(GIT_DIRTY)"

help: ## Show all available targets
	@printf "Usage: make <target> [VARIABLE=value]\n\n"
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-28s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@printf "\nEvaluation variables:\n"
	@printf "  SUITE=all|evaluation|policy|grounding|injection|guardrails   RUN_NAME=<name>   FAIL_THRESHOLD=1.0\n"
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

wait: ## Wait for UI, gateway, Keycloak, MCP Inspector, MLflow, and Collector readiness
	@deadline=$$((SECONDS + $(WAIT_TIMEOUT))); \
	printf "Waiting up to $(WAIT_TIMEOUT)s for the secured cluster"; \
	until \
		curl -fsS http://localhost:3000/ >/dev/null 2>&1 && \
		$(COMPOSE) exec -T gateway curl -fsS http://127.0.0.1:8081/ready >/dev/null 2>&1 && \
		curl -fsS http://localhost:8082/realms/$${KEYCLOAK_REALM:-etf-research}/.well-known/openid-configuration >/dev/null 2>&1 && \
		curl -fsS http://localhost:$(MLFLOW_PORT)/health >/dev/null 2>&1 && \
		curl -fsS http://localhost:6274/ >/dev/null 2>&1 && \
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
	@curl -fsS http://localhost:8082/realms/$${KEYCLOAK_REALM:-etf-research}/.well-known/openid-configuration >/dev/null && echo "OK  Keycloak       http://localhost:8082"
	@curl -fsS http://localhost:$(MLFLOW_PORT)/health >/dev/null && echo "OK  MLflow         http://localhost:$(MLFLOW_PORT)"
	@curl -fsS http://localhost:6274/ >/dev/null && echo "OK  MCP Inspector  http://localhost:6274"
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

logs-inspector: ## Follow MCP Inspector logs
	$(COMPOSE) logs -f --tail=$(LOG_TAIL) mcp-inspector

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

verify-hitl: ## Verify a human can INITIATE a decision override end to end (needs the cluster and a model)
	@echo "Resetting $(HITL_TEST_ETF)"
	@$(COMPOSE) exec -T postgres psql -q -U "$(POSTGRES_USER)" -d "$(POSTGRES_DB)" -c \
		"UPDATE etfs SET review_state='UNREVIEWED', decision=NULL, investment_score=NULL, \
		 decided_rules_version=NULL, decided_profile_version=NULL, assigned_to=NULL, \
		 research_note=NULL, updated_at=NOW() WHERE etf_id IN ($(HITL_TEST_ETF));"
	$(COMPOSE) exec -T agent python /app/verify_hitl_override.py
	@$(MAKE) --no-print-directory verify-hitl-audit

verify-hitl-audit: ## Assert the persisted history record of the human-initiated override
	@$(COMPOSE) exec -T postgres psql -qAt -U "$(POSTGRES_USER)" -d "$(POSTGRES_DB)" -c \
		"SELECT actor_type||'|'||rules_decision||'|'||COALESCE(llm_recommendation,'none')||'|'|| \
		 final_decision||'|'||override_applied::int||'|'||(length(override_rationale)>0)::int \
		 FROM audit_events WHERE etf_id IN ($(HITL_TEST_ETF)) AND action='EVALUATION_COMMITTED' \
		 ORDER BY id DESC LIMIT 1;" | { \
		read -r row; \
		echo "  history: $$row"; \
		IFS='|' read -r actor rules llm final override rationale <<< "$$row"; \
		fail=0; \
		[[ "$$actor" == "human" ]] || { echo "  [FAIL] actor_type=$$actor, expected human"; fail=1; }; \
		[[ "$$rules" == "research" ]] || { echo "  [FAIL] rules_decision=$$rules, expected research"; fail=1; }; \
		[[ "$$final" == "shortlist" ]] || { echo "  [FAIL] final_decision=$$final, expected shortlist"; fail=1; }; \
		[[ "$$override" == "1" ]] || { echo "  [FAIL] override_applied=$$override, expected 1"; fail=1; }; \
		[[ "$$rationale" == "1" ]] || { echo "  [FAIL] no override rationale was recorded"; fail=1; }; \
		[[ "$$llm" != "shortlist" ]] || { echo "  [FAIL] llm_recommendation=$$llm: the model proposed the promotion itself"; fail=1; }; \
		if [[ $$fail == 0 ]]; then \
			echo "  [PASS] human override recorded, and the model never proposed the promotion (llm=$$llm)"; \
		else exit 1; fi; }

verify-approvals: ## Verify the MCP human-approval boundary: signature, binding, replay, hard constraints
	@echo "Resetting the dedicated approval-test ETFs (audit_events is append-only and is kept)"
	@$(COMPOSE) exec -T postgres psql -q -U "$(POSTGRES_USER)" -d "$(POSTGRES_DB)" -c \
		"UPDATE etfs SET review_state='UNREVIEWED', decision=NULL, investment_score=NULL, \
		 decided_rules_version=NULL, decided_profile_version=NULL, assigned_to=NULL, \
		 research_note=NULL, updated_at=NOW() WHERE etf_id IN ($(APPROVAL_TEST_ETFS));"
	$(COMPOSE) exec -T agent python /app/verify_approval_tokens.py

diagrams: ## Re-render docs/img/*.png from the Mermaid sources embedded in README.md
	@mkdir -p docs/img /tmp/etf-research-mmd
	@python3 scripts/extract_diagrams.py /tmp/etf-research-mmd
	@for f in /tmp/etf-research-mmd/*.mmd; do \
		name=$$(basename "$$f" .mmd); \
		docker run --rm -v /tmp/etf-research-mmd:/in -v "$$PWD/docs/img:/out" minlag/mermaid-cli \
			-i "/in/$$name.mmd" -o "/out/$$name.png" -w 1400 -b white >/dev/null; \
		echo "  rendered docs/img/$$name.png"; \
	done

etf-check: ## Validate the ETF fixtures, investor profile, rules spec, and labelled cases
	python3 scripts/validate_etf_fixtures.py

rules-test: ## Test the shipped Rust evaluation engine against the fixtures; regenerates the baseline
	cd mcp-server && cargo test

static-check: etf-check verify-stream-adapter ## Run offline checks that need no Docker, cluster, Ollama or Rust (needs python3 and node on the host)
	python3 -m compileall -q agent/src evaluation scripts
	python3 -m unittest discover -s evaluation/tests -p 'test_*.py' -v
	python3 scripts/verify_security_sources.py

verify-input-guardrails: ## Run the input-guardrail regression smoke test
	$(COMPOSE) exec agent python /app/verify_input_guardrails.py

verify-stream-adapter: ## Verify the NAT SSE adapter preserves numeric/scalar answer chunks
	cd ui && node --experimental-strip-types scripts/verify-nat-wire.mjs

verify-output-guardrails: ## Verify streamed ETF research output plus secret blocking
	$(COMPOSE) exec agent python /app/verify_output_guardrails.py

verify-rails: ## Run the live NeMo Guardrails input/output rail regression suite
	$(COMPOSE) exec agent python /app/verify_guardrails_rails.py

verify-guardrails: verify-input-guardrails verify-output-guardrails verify-rails ## Run input + output guardrail regression checks

verify-trace-pipeline: ## Run the offline observability pipeline regression tests
	$(COMPOSE) exec -T agent python /app/verify_trace_pipeline.py

trace-test: verify-trace-pipeline ## Verify observability end to end against MLflow (needs the cluster and a model)
	python3 scripts/verify_traces_e2e.py

traces: ## Print the span tree of the most recent MLflow traces
	python3 scripts/inspect_mlflow_traces.py --limit $${LIMIT:-3}

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
	$(OPEN) http://localhost:$(MLFLOW_PORT)

open-inspector: ## Open MCP Inspector (token is read from .env/default)
	@token="$${MCP_INSPECTOR_API_TOKEN:-dev-mcp-inspector-token-change-me}"; \
	$(OPEN) "http://localhost:6274/?MCP_INSPECTOR_API_TOKEN=$$token"

inspector-tools: ## List MCP tools directly through Inspector CLI, bypassing NAT/gateway/UI
	$(COMPOSE) exec -T mcp-inspector sh -lc 'mcp-inspector --cli http://mcp-server:8080/mcp --transport http --header "Authorization: Bearer $$MCP_API_KEY" --method tools/list'

open-all: ## Open assistant-ui, Keycloak admin, and MLflow
	@$(MAKE) --no-print-directory open-ui
	@$(MAKE) --no-print-directory open-keycloak
	@$(MAKE) --no-print-directory open-mlflow

login-info: ## Print the development login URLs and credentials
	@printf "UI:               http://localhost:3000\n"
	@printf "MCP Inspector:    http://localhost:6274  (make open-inspector)\n"
	@printf "Keycloak admin:   http://localhost:8082/admin/  (%s / %s)\n" "$${KEYCLOAK_ADMIN_USERNAME:-admin}" "$${KEYCLOAK_ADMIN_PASSWORD:-admin}"
	@printf "Demo researcher:  %s / %s\n" "$${KEYCLOAK_RESEARCHER_USERNAME:-researcher}" "$${KEYCLOAK_RESEARCHER_PASSWORD:-researcher}"

security-config-test: ## Validate the resolved Compose topology and security-critical source wiring
	$(EVAL_COMPOSE) config --format json | python3 scripts/verify_security_config.py
	python3 scripts/verify_security_sources.py

auth-test: ## Smoke-test Keycloak discovery and every authentication boundary
	@set -euo pipefail; \
	realm="$${KEYCLOAK_REALM:-etf-research}"; \
	login_cookie="$${GATEWAY_LOGIN_COOKIE:-etf_research_gateway_login}"; \
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
	echo "Authentication boundary smoke tests passed. (MCP key boundary: make verify-mcp)"

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

eval-bootstrap: ## Create or merge MLflow datasets; choose with SUITE=all|evaluation|policy|grounding|injection|guardrails
	$(EVAL_COMPOSE) run --rm evaluator python -m evaluation bootstrap --suite $(SUITE)

eval-bootstrap-replace: ## Replace selected MLflow dataset records from checked-in JSON
	$(EVAL_COMPOSE) run --rm evaluator python -m evaluation bootstrap --suite $(SUITE) --replace

eval-bootstrap-etf: ## Synchronize the labelled ETF evaluation cases
	@$(MAKE) --no-print-directory eval-bootstrap SUITE=evaluation

eval-bootstrap-policy: ## Synchronize decision-policy evaluation cases
	@$(MAKE) --no-print-directory eval-bootstrap SUITE=policy

eval-bootstrap-grounding: ## Synchronize research-grounding evaluation cases
	@$(MAKE) --no-print-directory eval-bootstrap SUITE=grounding

eval-bootstrap-guardrails: ## Synchronize prompt-robustness evaluation cases
	@$(MAKE) --no-print-directory eval-bootstrap SUITE=guardrails

eval: ## Run a live MLflow evaluation; configure with SUITE, RUN_NAME, and other variables
	$(EVAL_COMPOSE) run --rm evaluator python -m evaluation run $(EVAL_RUN_ARGS)

eval-etf: ## Run the labelled deterministic-evaluation accuracy suite
	@$(MAKE) --no-print-directory eval SUITE=evaluation

eval-policy: ## Run decision-policy enforcement evaluation
	@$(MAKE) --no-print-directory eval SUITE=policy

eval-grounding: ## Run grounded research-explanation evaluation
	@$(MAKE) --no-print-directory eval SUITE=grounding

eval-guardrails: ## Run prompt robustness / Guardrails evaluation
	@$(MAKE) --no-print-directory eval SUITE=guardrails

eval-injection: ## Run the data-plane prompt-injection suite (poisons ETF metadata, then restores it)
	@echo "Injecting adversarial text into ETF metadata (fixtures on disk are untouched)"
	@python3 scripts/poison_etf_metadata.py --inject | $(COMPOSE) exec -T postgres psql -q -U "$(POSTGRES_USER)" -d "$(POSTGRES_DB)"
	@set +e; $(MAKE) --no-print-directory eval SUITE=injection; status=$$?; \
		echo "Restoring the shipped ETF metadata"; \
		python3 scripts/poison_etf_metadata.py --restore | $(COMPOSE) exec -T postgres psql -q -U "$(POSTGRES_USER)" -d "$(POSTGRES_DB)"; \
		exit $$status

# The injection suite is only meaningful against poisoned metadata: run clean, its
# cases pass trivially because there is no attack present. Any aggregate run must
# therefore poison and restore around itself. The poisoned ETFs are disjoint from
# every other dataset, so the other suites are unaffected.
# Expected to exit non-zero on the current model: the policy and grounding gates
# are deliberately strict and `qwen3:8b` does not clear them. That is a published
# finding, not a broken build — see docs/EVALUATION_ANALYSIS.md. Use
# `eval-all-allow-failures` when you want the artifacts without the exit status.
eval-all: ## Run all five suites with strict gates; exits non-zero while any gate is red
	@$(MAKE) --no-print-directory eval-suite-all SUITE=all ALLOW_FAILURES=0

eval-all-allow-failures: ## Run all suites but never fail the shell on a metric gate
	@$(MAKE) --no-print-directory eval-suite-all SUITE=all ALLOW_FAILURES=1

# SUITE is honoured rather than hardcoded, so a manual or CI run can poison,
# evaluate one suite, and restore through the same path. It defaults to `all`.
eval-suite-all:
	@python3 scripts/poison_etf_metadata.py --inject | $(COMPOSE) exec -T postgres psql -q -U "$(POSTGRES_USER)" -d "$(POSTGRES_DB)"
	@set +e; $(MAKE) --no-print-directory eval SUITE=$(SUITE) ALLOW_FAILURES=$(ALLOW_FAILURES); status=$$?; \
		python3 scripts/poison_etf_metadata.py --restore | $(COMPOSE) exec -T postgres psql -q -U "$(POSTGRES_USER)" -d "$(POSTGRES_DB)"; \
		exit $$status

eval-test: ## Run evaluator parser and deterministic-scorer unit tests
	$(EVAL_COMPOSE) run --rm evaluator python -m unittest discover -s evaluation/tests -p 'test_*.py' -v

test: etf-check rules-test eval-test verify-guardrails verify-trace-pipeline security-test verify-approvals ## Run fixture, engine, evaluator, Guardrails, observability, security, and approval-boundary tests
