# Configuration

Every variable has a working default in `docker-compose.yml`, so `make dev`
starts without setting any of them. `.env.example` documents the ones that
matter.

Secrets and endpoints come from the environment; the agent's behaviour comes
from [`agent/config.yml`](#agent-behaviour-agentconfigyml). Three variables were
renamed when the Rust agent became canonical; the old names still work for now
([renamed variables](#renamed-variables)).

## Model endpoint

Any OpenAI-compatible endpoint. The defaults target a local Ollama running
`qwen3:8b`; nothing else depends on Ollama.

| Variable | Default | Notes |
| --- | --- | --- |
| `LLM_BASE_URL` | `http://host.docker.internal:11434/v1` | |
| `LLM_API_KEY` | `ollama` | |
| `LLM_MODEL` | `qwen3:8b` | |
| `LLM_GUARD_MODEL` | `qwen3:8b` | Does **not** inherit `LLM_MODEL` |
| `LLM_REASONING_EFFORT` | `none` | Empty ⇒ omitted entirely |
| `LLM_GUARD_REASONING_EFFORT` | `none` | Empty ⇒ omitted entirely |

To use a hosted provider: set the base URL, key and model names, empty the two
reasoning settings, and `make dev`. `make pull-models` becomes a no-op — it only
runs when `LLM_BASE_URL` is an Ollama endpoint.

The guard model's fallback chain ends at the literal `qwen3:8b`, so an endpoint
that does not serve that name **must** set `LLM_GUARD_MODEL` or the input rail
fails on the first request. `LLM_GUARD_MODEL` is independent of `LLM_MODEL` in
both directions: changing the primary model below does not change which model
classifies input, and vice versa.

### `qwen3:8b` and the prioritization prompt

> **With the current Rig agent** the same model answered this prompt correctly
> from a single `search_tickets` call in the runs observed, and the evaluation
> suites passed at their gates. That is an observation from a handful of runs,
> not evidence that the runtime fixed a model limitation: the model and prompt
> are the same, but the two clients build different request bodies. What
> follows was observed with the retired NAT agent
> ([FRAMEWORK-MIGRATION.md](FRAMEWORK-MIGRATION.md#what-was-measured)); keep it in
> mind when a small model stops mid-trajectory.

In repeated local testing against this support-ticket example, `qwen3:8b`
answers the single-tool and no-tool demonstration prompts correctly and
consistently (searching tickets, fetching one ticket, summarizing a ticket's
history). It consistently failed to produce a final answer for "Which ticket
should we handle first, and why?": instead of reasoning from the ticket list
`search_tickets` already returns, it called `get_ticket` on every open ticket
and then stopped without ever emitting closing text. The agent, MCP server and
guardrails all behaved correctly throughout — the tool calls, their arguments,
and the underlying data were all correct; only the model's final response was
missing. This reads as a small local model's tool-orchestration limit on a
longer multi-tool trajectory, not a defect in the application, though a model
swap does not by itself rule out every other explanation.

`qwen3.5:9b`, served by the same local Ollama, answered the identical prompt
correctly and consistently (it did not even need the `get_ticket` fan-out —
`search_tickets`'s own result was enough). To try it:

```bash
# in .env
LLM_MODEL=qwen3.5:9b
LLM_GUARD_MODEL=qwen3.5:9b
```

then `ollama pull qwen3.5:9b` (or `make pull-models` if `LLM_MODEL` is already
set) and `make dev`. This is not a recommendation to change the shipped
default — `qwen3:8b` remains what the template ships and is smaller/cheaper to
run — only a confirmed working alternative for this specific prompt.

### Empty means omitted

`reasoning_effort` is not universally valid: local Qwen3 needs
`reasoning_effort: none` to suppress thinking (without it the short Yes/No
guardrail classification breaks), while many OpenAI-compatible endpoints reject
the parameter outright. So a set-but-empty `LLM_REASONING_EFFORT` (or
`LLM_GUARD_REASONING_EFFORT`) means *do not send the parameter at all*. Compose
preserves the emptiness with `${LLM_REASONING_EFFORT-none}` (`-`, not `:-`), the
agent parses it into `None` ([`config.rs`](../agent/src/config.rs)), and
`reasoning_effort` is sent only when it is `Some`
([`agent/model.rs`](../agent/src/agent/model.rs)).

Absence is a value worth modelling explicitly. The retired NAT agent needed a
custom provider for the same rule, because its YAML interpolation could not
express it and sent the string `"null"` instead; see
[FRAMEWORK-MIGRATION.md](FRAMEWORK-MIGRATION.md#model-client).

The model client is Rig's generic OpenAI dialect on Chat Completions, with
strict tool mode off (it would mark optional parameters required); verified
against Ollama with `qwen3:8b`.

## Agent behaviour (`agent/config.yml`)

Behaviour comes from `agent/config.yml`: `workflow.system_prompt` and the loop
bounds, `tools.mcp.include` and `tools.mcp.overrides`, `tools.approval`
(commented out by default), `guardrails.prompts`,
`guardrails.output.secret_patterns` and `guardrails.output.pii_entities`.
Unknown keys are refused, and the file is baked into the image, so edit it and
`make rebuild-agent`. Typed parsing is in [`config.rs`](../agent/src/config.rs).

**Fail fast.** Any security-relevant setting that does not parse stops startup:
a missing credential, a short approval secret, an unrecognised boolean, a
partially enabled approval feature, a secret pattern that does not compile, an
unknown PII entity, an MCP tool schema the agent cannot enforce, or a renamed
variable whose old and new names disagree.

| Agent setting | Default | Effect |
| --- | --- | --- |
| `HITL_INTERACTION_TIMEOUT_SECONDS` | `600` | a pending approval older than this counts as cancelled (floor 5) |
| `AGENT_TRACE_CAPTURE_CONTENT` | `true` | readable question and answer on the request's root span ([OBSERVABILITY.md](OBSERVABILITY.md)) |
| `AGENT_TRACE_CONTENT_MAX_CHARS` | `65536` | bound on that content (floor 256) |
| `AGENT_TRACE_CAPTURE_MODEL_CONTENT` | `false` | Rig's own prompt/completion span content |
| `AGENT_CONFIG_PATH`, `AGENT_BIND_ADDRESS` | `/app/config.yml`, `0.0.0.0:8000` | |
| `AGENT_RUST_LOG` | `tickets_agent=info,warn` | the agent container's `RUST_LOG` (container log only) |

Accepted and without effect: `HITL_STRICT_INTERACTION_OWNERSHIP` (ownership is
always enforced; `false` prints a startup warning). The trusted identity header is the
constant `x-authenticated-user-id` ([`identity.rs`](../agent/src/identity.rs)),
not a setting.

## Renamed variables

The agent's environment once used the names of the NeMo Agent Toolkit runtime
it was first built on. They were renamed to agent-neutral names when the Rust
agent became canonical:

| Current name | Deprecated alias | Where the old name lived |
| --- | --- | --- |
| `AGENT_API_KEY` | `NAT_GATEWAY_API_KEY` | inside the agent container only; users always set `AGENT_API_KEY`, which Compose passed to the gateway and evaluator under that name and to the agent as `NAT_GATEWAY_API_KEY` |
| `AGENT_TRACE_CAPTURE_CONTENT` | `NAT_TRACE_CAPTURE_CONTENT` | user-facing: `.env` and the agent container |
| `AGENT_TRACE_CONTENT_MAX_CHARS` | `NAT_TRACE_CONTENT_MAX_CHARS` | user-facing: `.env` and the agent container |

How an alias is resolved depends on where it is set.

**In the agent process** (its own environment: a container run without this
Compose file, Kubernetes, or a local `cargo run`), the agent sees both names
and applies one rule, explicit and tested (`config::RENAMED` in
[`config.rs`](../agent/src/config.rs)):

1. the current name, when set and non-blank, always wins;
2. otherwise the deprecated alias is used, and startup prints a deprecation
   warning naming the replacement to stderr (`tickets-agent warning: …`;
   configuration is read before tracing starts);
3. if both are set to **different** values, the agent refuses to start: one of
   them is a stale credential or setting that someone believes is in effect.

**Through this Compose file**, the agent container receives only the current
names; `make security-config-test` fails if it ever receives a deprecated one.

* The two trace variables are resolved in `docker-compose.yml` before the
  agent sees them:
  `${AGENT_TRACE_CAPTURE_CONTENT:-${NAT_TRACE_CAPTURE_CONTENT:-true}}`. An old
  `.env` that sets only `NAT_TRACE_*` keeps working, but silently: the agent
  receives an `AGENT_*` value and prints no deprecation warning. If `.env` sets
  both names to different values, Compose takes the `AGENT_*` one; the agent
  cannot see the conflict, so it is not rejected. Rename them in `.env`.
* The service key was never a host-side `NAT_*` variable. Compose reads
  `AGENT_API_KEY` from `.env` and now passes it to the agent under the same
  name, so the gateway, the evaluator and the agent provably share one value
  (`make security-config-test` asserts it). A `NAT_GATEWAY_API_KEY` in `.env` is
  ignored by Compose, as it always was.

**Removal.** The aliases exist for one deprecation cycle so existing `.env`
files and deployments keep working. They will be removed in a later release;
rename them now. A deployment that sets only the current names is unaffected.

The other environment names (`LLM_*`, `MCP_API_KEY`, `HITL_*`, `GUARDRAILS_*`,
`OTEL_*`) were already framework-neutral and did not change.

## Project and volume identity

`docker-compose.yml` pins the project name (`tickets-agent` by default) so
container, network and volume names do not derive from the clone directory.
`COMPOSE_PROJECT_NAME` and `docker compose -p` both override it, and that
override reaches the persistent volumes too: `postgres-data`,
`phoenix-data`, `keycloak-data`, `keycloak-import` and (with the `mlflow`
profile) `mlflow-data` each default to
`${the-effective-project-name}-<volume>`, computed from whichever of `-p`,
`COMPOSE_PROJECT_NAME`, or the `tickets-agent` default actually won for that
invocation. That is what lets a template checkout and a domain fork run side
by side on one Docker host with genuinely separate storage, not only separate
containers — setting the project name once is enough; nothing else needs to
change per project.

Each volume's computed default is still overridable
(`POSTGRES_DATA_VOLUME`, `PHOENIX_DATA_VOLUME`, `MLFLOW_DATA_VOLUME`,
`KEYCLOAK_DATA_VOLUME`, `KEYCLOAK_IMPORT_VOLUME`) for a deployment that wants one specific, stable
name regardless of project — an explicit override always wins over the
computed default.

## Host ports

Every published port binds to loopback by default (`PUBLIC_BIND_ADDRESS`).
The ones a person opens have overridable host ports; container ports never
change, so nothing inside the cluster is affected.

| Variable | Default | Service |
| --- | --- | --- |
| `UI_PORT` | `3000` | assistant-ui (set `UI_PUBLIC_URL` to match) |
| `KEYCLOAK_PORT` | `8082` | Keycloak (set `KEYCLOAK_PUBLIC_URL` to match) |
| `PHOENIX_PORT` | `6006` | Phoenix UI and REST API (also TensorBoard's default) |
| `MLFLOW_PORT` | `5000` | optional MLflow; 5000 is held by macOS AirPlay Receiver |

The collector publishes 4318 (OTLP/HTTP) and 13133 (health) on loopback.
Phoenix's OTLP/gRPC port 4317 is not published.

## Security-critical settings

| Variable | Default | Notes |
| --- | --- | --- |
| `MCP_API_KEY` | dev value | agent → MCP |
| `AGENT_API_KEY` | dev value | gateway/evaluator → agent; deprecated alias `NAT_GATEWAY_API_KEY` ([renamed variables](#renamed-variables)) |
| `KEYCLOAK_GATEWAY_CLIENT_SECRET` | dev value | |
| `GATEWAY_COOKIE_SECURE` | `false` | Set `true` behind TLS. Parsed strictly — a typo is an error, not silently `false` |
| `GATEWAY_SESSION_TTL_SECONDS` | `28800` | |
| `GATEWAY_MAX_STREAMS_PER_SESSION` | `4` | |
| `GATEWAY_UPSTREAM_TIMEOUT_SECONDS` | `10` | Non-streaming calls only |
| `HITL_APPROVAL_SECRET` | **unset** | Unset keeps the stack read-only; see [APPROVALS.md](APPROVALS.md) |
| `EVALUATION_PRINCIPAL` | `evaluation-harness` | Identity the evaluation harness asserts to the agent. Every direct caller must assert one; see [SECURITY.md](SECURITY.md#the-agent-requires-an-asserted-identity) |

The agent's trusted identity header is a constant in
[`identity.rs`](../agent/src/identity.rs) rather than an environment variable,
because it is a property of the deployment's trust boundary rather than a knob:
changing it means changing which header the agent believes, and that only makes
sense together with the proxy that mints it.

Guardrail and telemetry settings are in [GUARDRAILS.md](GUARDRAILS.md) and
[OBSERVABILITY.md](OBSERVABILITY.md). Domain bindings for the evaluation are in
[EVALUATION.md](EVALUATION.md#generalizing-for-a-domain-application).

## Evaluation (core)

What a suite is, its cases and its gate are defined in the repository
(`evaluation/config.py`, `evaluation/datasets/*.json`), not in the environment.
The variables below configure *how* the evaluator runs and where it publishes a
copy; none of them can change a score or a gate.

| Variable | Default | Notes |
| --- | --- | --- |
| `EVALUATION_REPORTERS` | `phoenix` (evaluator service), `phoenix,mlflow` (`evaluator-mlflow`) | Comma list of `phoenix`, `mlflow`, or `none`. The JSON result is written either way. `make eval EVAL_REPORTERS=...` overrides per run |
| `EVALUATION_HTTP_TIMEOUT_SECONDS` | `360` | Per agent call |
| `EVALUATION_HTTP_MAX_ATTEMPTS` | `4` | Retries on connection failure only |
| `EVALUATION_TOOL_NAMES` | `search_tickets,get_ticket` | Tool names recognised as tool calls |
| `EVALUATION_MUTATING_TOOLS` | empty | Tools that change state |
| `EVALUATION_PRINCIPAL` | `evaluation-harness` | See above |
| `*_EVALUATION_EXPERIMENT`, `*_EVALUATION_DATASET` | `tickets-agent-<suite>-…` | Display names a reporter uses for the experiment and the dataset mirror. They identify nothing |

## Phoenix

The default trace store and evaluation workbench, in the default stack. What it
is and how it ingests: [OBSERVABILITY.md](OBSERVABILITY.md#phoenix).

| Variable | Default | Notes |
| --- | --- | --- |
| `PHOENIX_PORT` | `6006` | Host port |
| `PHOENIX_PROJECT_NAME` | `tickets-agent` | Project the collector files agent spans under |
| `PHOENIX_PUBLIC_URL` | `http://localhost:6006` | Only used to print experiment links |
| `PHOENIX_API_KEY` | empty | Bearer token the evaluator sends, for a Phoenix with authentication enabled. Never logged or written to a result |
| `PHOENIX_DATA_VOLUME` | `<project>-phoenix-data` | See [volume identity](#project-and-volume-identity) |

The in-cluster address (`http://phoenix:6006`) is fixed, not configurable.

**Authentication is off** in the shipped stack: Compose sets
`PHOENIX_ENABLE_AUTH: "false"` as a literal, and it is deliberately *not* read
from `.env`. Phoenix rejects an empty `PHOENIX_SECRET` whenever that variable is
present at all, so the two cannot be passed through as empty defaults. To enable
authentication — required before Phoenix is reachable from anywhere but
loopback — set both together in a `docker-compose.override.yml` beside
`docker-compose.yml` (Compose loads it automatically whenever no `-f` is given,
which covers every `make` target except `debug-up`/`debug-down`):

```yaml
services:
  phoenix:
    environment:
      PHOENIX_ENABLE_AUTH: "true"
      PHOENIX_SECRET: "<32+ random characters, with at least one digit and one lowercase letter>"
```

Then create a system API key in Phoenix and give it to the evaluator as
`PHOENIX_API_KEY`. Phoenix's own usage telemetry stays disabled either way.

## Optional MLflow

A reference integration, not part of the default stack. `make dev-mlflow`
starts the `mlflow` profile (the `mlflow` service) and recreates the collector
with `OTEL_COLLECTOR_OVERLAY=otel-collector.mlflow.yml`. The `evaluator-mlflow`
service has its own `evaluation-mlflow` profile, like `evaluator` has
`evaluation`, so bringing the stack up never starts an evaluation run;
`make eval-mlflow` runs it by name.

| Variable | Default | Notes |
| --- | --- | --- |
| `OTEL_COLLECTOR_OVERLAY` | `otel-collector.default.yml` (adds nothing) | Collector overlay file in `observability/`. Set by `make dev-mlflow`/`up-mlflow`; a bare `docker compose --profile mlflow up` without it runs MLflow with no agent traces, which `make health` reports |
| `MLFLOW_PORT` | `5000` | Host port |
| `MLFLOW_TRACKING_URI` | `http://mlflow:5000` | Used by the MLflow reporter |
| `EVALUATION_MODEL_PREFIX` | `agent` | How the deployed agent is grouped as an MLflow model |
| `MLFLOW_DATA_VOLUME` | `<project>-mlflow-data` | Created only when the profile first starts |

## Optional development profiles

| Profile | Command | What |
| --- | --- | --- |
| `dev` | `make inspector` | MCP Inspector, loopback-bound and token-authenticated |
| `evaluation` | `make eval` | The backend-neutral evaluator container (Python + PyYAML) |
| `mlflow` | `make dev-mlflow`, `make eval-mlflow` | Optional MLflow server and the MLflow-image evaluator; see [optional MLflow](#optional-mlflow) |

The Inspector holds the real MCP service credential, so an unauthenticated one
would bypass the MCP boundary outright; it is token-gated and never started by
default.
