# Configuration

Every variable has a working default in `docker-compose.yml`, so `make dev`
starts without setting any of them. `.env.example` documents the ones that
matter.

Both agent implementations read the **same variable names**, including the
historical `NAT_*` ones, so the two branches' configurations compare line by
line. Where the Rig agent on `rust-agent` reads or interprets something
differently, [On the Rig implementation](#on-the-rig-implementation) says so.

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

> **On the Rig implementation** the same model answered this prompt correctly
> from a single `search_tickets` call in the runs observed, and the evaluation
> suites passed at their gates. That is an observation from a handful of runs,
> not evidence that the runtime fixed a model limitation: the model and prompt
> are the same, but the two clients build different request bodies. What
> follows was observed with the NAT agent.

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

### Why "empty means omitted" needed code

`reasoning_effort` is not universally valid: local Qwen3 needs
`reasoning_effort: none` to suppress thinking (without it the short Yes/No
guardrail classification breaks), while many OpenAI-compatible endpoints reject
the parameter outright.

NAT's YAML interpolation cannot express absence. `${VAR:-default}` always
produces a string, for both an unset and an explicitly empty variable, and
`OpenAIModelConfig` allows extra fields and forwards any key written in the YAML.
Writing `reasoning_effort: ${LLM_REASONING_EFFORT:-null}` does not omit the
parameter — it sends the four-character string `"null"`, which is worse than
sending nothing, because it is a value the provider must reject. Measured:

```
explicit 'null'  -> reasoning_effort in client kwargs: True   value='null'
explicit ''      -> reasoning_effort in client kwargs: True   value=''
explicit 'none'  -> reasoning_effort in client kwargs: True   value='none'
omitted          -> reasoning_effort in client kwargs: False
```

So `agent/src/nat_streaming_react/llm_config.py` registers an
`openai_optional_params` provider that drops empty optional parameters before
pydantic records them as set. `TextGuardrailsMiddlewareConfig` applies the same
rule to the guard model's `extra_body`.

## On the Rig implementation

Behaviour comes from `agent/config.yml`, whose layout differs from NAT's (the
prompts in it are byte-identical): `workflow.system_prompt`,
`tools.mcp.include` and `tools.mcp.overrides`, `tools.approval` (commented out
by default), `guardrails.prompts`, `guardrails.output.secret_patterns` and
`guardrails.output.pii_entities`. Unknown keys are refused, and the file is
baked into the image, so edit it and `make rebuild-agent`. Typed parsing is in
[`config.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/config.rs).

**Fail fast.** Any security-relevant setting that does not parse stops startup:
a missing credential, a short approval secret, an unrecognised boolean (NAT
keeps the default and warns), a partially enabled approval feature, a secret
pattern that does not compile, an unknown PII entity, or an MCP tool schema the
agent cannot enforce.

**Empty means omitted** is one `Option` there: a set-but-empty
`LLM_REASONING_EFFORT` becomes `None`, and `reasoning_effort` is sent only when it
is `Some` ([`agent/model.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/agent/model.rs)). The model client is
Rig's generic OpenAI dialect on Chat Completions, with strict tool mode off
(it would mark optional parameters required); verified against Ollama with
`qwen3:8b`.

| Rig-only setting | Default | Effect |
| --- | --- | --- |
| `HITL_INTERACTION_TIMEOUT_SECONDS` | `600` | a pending approval older than this counts as cancelled (floor 5) |
| `AGENT_TRACE_CAPTURE_MODEL_CONTENT` | `false` | Rig's own prompt/completion span content |
| `AGENT_CONFIG_PATH`, `AGENT_BIND_ADDRESS` | `/app/config.yml`, `0.0.0.0:8000` | |
| `AGENT_RUST_LOG` | `tickets_agent=info,warn` | the agent container's `RUST_LOG` (container log only) |

Read by NAT and ignored by Rig: `GUARDRAILS_RAIL_POOL_SIZE` (no rails pool) and
`HITL_STRICT_INTERACTION_OWNERSHIP` (ownership is always enforced). The trusted
identity header is a constant in the Rig agent rather than NAT's
`general.front_end.identity_header`.

## Project and volume identity

`docker-compose.yml` pins the project name (`tickets-agent` by default) so
container, network and volume names do not derive from the clone directory.
`COMPOSE_PROJECT_NAME` and `docker compose -p` both override it, and that
override reaches the four persistent volumes too: `postgres-data`,
`mlflow-data`, `keycloak-data` and `keycloak-import` each default to
`${the-effective-project-name}-<volume>`, computed from whichever of `-p`,
`COMPOSE_PROJECT_NAME`, or the `tickets-agent` default actually won for that
invocation. That is what lets a template checkout and a domain fork run side
by side on one Docker host with genuinely separate storage, not only separate
containers — setting the project name once is enough; nothing else needs to
change per project.

Each volume's computed default is still overridable
(`POSTGRES_DATA_VOLUME`, `MLFLOW_DATA_VOLUME`, `KEYCLOAK_DATA_VOLUME`,
`KEYCLOAK_IMPORT_VOLUME`) for a deployment that wants one specific, stable
name regardless of project — an explicit override always wins over the
computed default.

## Host ports

Every published port binds to loopback by default (`PUBLIC_BIND_ADDRESS`). Only
MLflow's host port is variable (`MLFLOW_PORT`), because 5000 is the one that
reliably collides — macOS AirPlay Receiver holds it, as does any other MLflow on
the machine. The container port stays 5000, so nothing inside the cluster
changes.

## Security-critical settings

| Variable | Default | Notes |
| --- | --- | --- |
| `MCP_API_KEY` | dev value | agent → MCP |
| `AGENT_API_KEY` | dev value | gateway/evaluator → agent (both agents read it as `NAT_GATEWAY_API_KEY`) |
| `KEYCLOAK_GATEWAY_CLIENT_SECRET` | dev value | |
| `GATEWAY_COOKIE_SECURE` | `false` | Set `true` behind TLS. Parsed strictly — a typo is an error, not silently `false` |
| `GATEWAY_SESSION_TTL_SECONDS` | `28800` | |
| `GATEWAY_MAX_STREAMS_PER_SESSION` | `4` | |
| `GATEWAY_UPSTREAM_TIMEOUT_SECONDS` | `10` | Non-streaming calls only |
| `HITL_APPROVAL_SECRET` | **unset** | Unset keeps the stack read-only; see [APPROVALS.md](APPROVALS.md) |
| `EVALUATION_PRINCIPAL` | `evaluation-harness` | Identity the evaluation harness asserts to the agent. Every direct caller must assert one; see [SECURITY.md](SECURITY.md#the-agent-requires-an-asserted-identity) |

The agent's trusted identity header is set in `agent/config.yml`
(`general.front_end.identity_header`) rather than by environment variable,
because it is a property of the deployment's trust boundary rather than a knob:
changing it means changing which header the agent believes, and that only makes
sense together with the proxy that mints it.

Guardrail and telemetry settings are in [GUARDRAILS.md](GUARDRAILS.md) and
[OBSERVABILITY.md](OBSERVABILITY.md). Evaluation bindings are in
[EVALUATION.md](EVALUATION.md).

## Optional development profiles

| Profile | Command | What |
| --- | --- | --- |
| `dev` | `make inspector` | MCP Inspector, loopback-bound and token-authenticated |
| `evaluation` | `make eval` | The evaluator container |

The Inspector holds the real MCP service credential, so an unauthenticated one
would bypass the MCP boundary outright; it is token-gated and never started by
default.
