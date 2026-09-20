# Configuration

Every variable has a working default in `docker-compose.yml`, so `make dev`
starts without setting any of them. `.env.example` documents the ones that
matter.

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
| `MCP_API_KEY` | dev value | NAT → MCP |
| `AGENT_API_KEY` | dev value | gateway/evaluator → NAT (NAT reads `NAT_GATEWAY_API_KEY`) |
| `KEYCLOAK_GATEWAY_CLIENT_SECRET` | dev value | |
| `GATEWAY_COOKIE_SECURE` | `false` | Set `true` behind TLS. Parsed strictly — a typo is an error, not silently `false` |
| `GATEWAY_SESSION_TTL_SECONDS` | `28800` | |
| `GATEWAY_MAX_STREAMS_PER_SESSION` | `4` | |
| `GATEWAY_UPSTREAM_TIMEOUT_SECONDS` | `10` | Non-streaming calls only |
| `HITL_APPROVAL_SECRET` | **unset** | Unset keeps the stack read-only; see [APPROVALS.md](APPROVALS.md) |

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
