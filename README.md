# assistant-ui + NAT ReAct + Rust MCP + Postgres

> **NAT ReAct prompt compatibility:** the custom `system_prompt` contains the required `{tools}` and `{tool_names}` placeholders. NAT replaces these at startup with the discovered MCP tool descriptions and names.


This POC demonstrates a complete local agent path:

```text
assistant-ui
    ↓ AI SDK UI message stream
Next.js adapter
    ↓ POST /v1/workflow/full
NeMo Agent Toolkit ReAct workflow
    ↓ MCP client (streamable HTTP)
Rust MCP server
    ↓ SQLx
Postgres

NAT + Guardrails traces
    ↓ OTLP/HTTP
OpenTelemetry Collector
    ↓
MLflow
```

The agent keeps NeMo Guardrails as input/output middleware and uses Ollama
through its OpenAI-compatible endpoint.

## Demonstrated scenarios

The seeded database contains two open alerts (`ALT-1001`, `ALT-1002`), one
ordinary closed alert (`ALT-1003`), and two synthetic closed guardrail fixtures.
Try these prompts in the browser:

1. **Show me my open alerts**
   - ReAct calls `search_alerts` with `status="open"`.
2. **Tell me more about alert ALT-1001**
   - ReAct calls `get_alert` with `alert_id="ALT-1001"`.
3. **Show all transactions for all open alerts**
   - ReAct first calls `search_alerts` with `status="open"`.
   - It then calls `get_alert` once for every returned alert ID.

The tool is named `get_alert` (singular), matching the MCP contract.

A complete prompt matrix covering MCP fan-out, tool errors, input blocking,
Presidio masking, regex blocking, streaming, and the one-trace acceptance test
is available in [`docs/TEST-SCENARIOS.md`](docs/TEST-SCENARIOS.md).

## Tool calls in assistant-ui

NAT's built-in ReAct OpenAI-compatible stream emits only the final answer. It
intentionally hides internal reasoning and tool chunks. To show tool activity,
the Next.js route calls:

```text
POST /v1/workflow/full?filter_steps=TOOL_START,TOOL_END
```

It translates NAT intermediate events into AI SDK UI message chunks:

- `TOOL_START` → `tool-input-available`
- `TOOL_END` → `tool-output-available`
- workflow output → streamed text parts

assistant-ui renders those parts as expandable tool cards containing the tool
name, input, running/completed status, and result.

## Rust MCP server

The server uses the official Rust MCP SDK (`rmcp`) with streamable HTTP at:

```text
http://mcp-server:8080/mcp
```

It exposes:

### `search_alerts`

Input:

```json
{
  "status": "open",
  "limit": 50
}
```

Output: JSON containing `count`, applied `filters`, and alert summaries.

### `get_alert`

Input:

```json
{
  "alert_id": "ALT-1001"
}
```

Output: JSON containing the complete alert, `transaction_count`, and all
associated transactions.

Both tools use parameterized SQL queries. The MCP server never accepts raw SQL.

## Makefile shortcuts

The root `Makefile` wraps the common Docker, observability, Guardrails, and
MLflow evaluation commands. Start with:

```bash
make help
```

For normal development, rebuild and restart the complete cluster with:

```bash
make dev
```

Useful targets include:

```text
make logs-app                 Application logs
make logs-observability       Agent, Collector, and MLflow logs
make health                   Check all public endpoints
make verify-mcp               List the MCP tools through NAT
make verify-guardrails        Run the input-guardrail smoke test
make eval-bootstrap-replace   Recreate both MLflow datasets
make eval-guardrails          Run the Guardrails evaluation
make eval-tools               Run the tool-calling evaluation
make eval-all                 Run both evaluation suites
```

The generic evaluation target also accepts variables:

```bash
make eval SUITE=guardrails RUN_NAME=guardrails-v2 FAIL_THRESHOLD=0.95
```

## Start

Ensure Ollama is running on the host and the configured model exists:

```bash
ollama pull qwen3:8b
```

Copy the environment file if you want to customize it:

```bash
cp .env.example .env
```

Build and start everything:

```bash
docker compose up -d --build
```

Open:

- assistant-ui: `http://localhost:3000`
- NAT Swagger UI: `http://localhost:8000/docs`
- Rust MCP health: `http://localhost:8080/health`
- MLflow traces: `http://localhost:5000`

Follow logs:

```bash
docker compose logs -f postgres mcp-server agent ui
```

## Verify the MCP connection

NAT 1.8 installs the MCP CLI with `nvidia-nat-mcp`:

```bash
docker compose exec agent \
  nat mcp client tool list \
  --url http://mcp-server:8080/mcp
```

The output should include:

```text
search_alerts
get_alert
```

You can also inspect the routes exposed by NAT in Swagger and invoke
`POST /v1/workflow/full` directly.

## Reset the seeded database

Postgres initialization scripts run only when the data directory is empty. To
recreate the demo database:

```bash
docker compose down -v
docker compose up -d --build
```

This deletes the POC Postgres volume.

## Rebuild efficiently

Do not use `--no-cache` during normal development. BuildKit caches Python,
Cargo, npm downloads, and Rust build artifacts.

Rebuild only the Rust MCP server:

```bash
docker compose build mcp-server
docker compose up -d --force-recreate mcp-server
```

Rebuild only the NAT agent:

```bash
docker compose build agent
docker compose up -d --force-recreate agent
```

## Dependency trade-off

The previous chat-only implementation was intentionally lean and did not
install `nvidia-nat-langchain`. NVIDIA's built-in NAT 1.8 `_type: react_agent`
is implemented inside that plugin and depends on LangGraph/LangChain.
Consequently, this ReAct variant installs:

```text
nvidia-nat[langchain]==1.8.0
nvidia-nat-security[guardrails]==1.8.0
nvidia-nat-mcp==1.8.0
```

This restores the official supported ReAct implementation but also restores the
broad NAT LangChain dependency set. The expensive Python dependency layer is
cached, and the compiler required by `annoy` remains confined to the builder
stage.

## Production notes

This is a local demonstration. Before production use, add:

- authentication and authorization between NAT and MCP;
- tenant/user scoping in every SQL query;
- secrets management instead of demo database credentials;
- database migrations rather than a one-time init script;
- pagination and response-size limits for transaction-heavy alerts;
- access controls, retention, and redaction for OpenTelemetry/MLflow data;
- a dedicated low-latency guard model rather than sharing the application LLM;
- explicit package/image digest pinning and vulnerability scanning.

## Docker Host-header validation

RMCP protects streamable-HTTP servers against DNS rebinding by accepting only loopback `Host` values by default. NAT connects over the Compose network with `Host: mcp-server:8080`, so this template configures an explicit allowlist through `MCP_ALLOWED_HOSTS`. Keep the list narrow in deployed environments; do not disable host validation globally.

The default allows the Compose service name and local development access:

```env
MCP_ALLOWED_HOSTS=mcp-server,mcp-server:8080,localhost,localhost:8080,127.0.0.1,127.0.0.1:8080,::1
```

## UI stream compatibility fix

This variant fixes two NAT 1.8 / assistant-ui bridge issues:

- NAT may serialize a `ChatResponseChunk` as a Python/Pydantic representation in
  `data.value`; the Next.js route now extracts `choices[0].delta.content` from
  both JSON and repr forms.
- MCP-backed calls may be reported as either `TOOL_*` or `FUNCTION_*`
  intermediate events. The bridge requests and maps both forms to AI SDK
  `tool-input-available` and `tool-output-available` chunks.

After replacing the project, only the UI image needs rebuilding:

```bash
docker compose build ui
docker compose up -d --force-recreate ui
```

## Live response streaming

The UI bridge normalizes each NAT `/v1/workflow/full` `data.value` chunk and
forwards it immediately as an AI SDK `text-delta`. Tool start/end events remain
in the same stream, so assistant-ui displays tool cards followed by a live
streaming final answer. The bridge no longer buffers the complete answer.

## Native-tool token streaming fix

This version restores a local NAT component under
`agent/src/nat_streaming_react/register.py`.

The built-in NAT 1.8 ReAct streamer waits for a textual `Final Answer:` marker.
With `use_native_tool_calling: true`, the model returns a normal assistant
message without that marker, so NAT emits the complete buffered answer as one
fallback chunk. The local `_type: streaming_react_agent` keeps the same NAT
ReAct graph, MCP tool loading, retries, and intermediate tool events, but emits
native assistant content chunks immediately.

Rebuild only the agent after applying this version:

```bash
docker compose build agent
docker compose up -d --force-recreate agent ui
docker compose logs -f agent
```

## Type-hint compatibility fix

The local component intentionally does not enable `from __future__ import annotations`. NAT 1.8 introspects nested workflow callbacks with `typing.get_type_hints`; postponed string annotations can fail to resolve names such as `ChatResponse` after NAT wraps the callback. Keeping runtime annotations concrete matches NAT's built-in ReAct registration and avoids that startup failure.

## Regex and Presidio output guardrails

This build replaces the LLM-based `self check output` rail with deterministic regex blocking and Presidio masking. See `docs/README-REGEX-PRESIDIO.md` for the policy, rebuild steps, and tuning details.

## Text-aware streaming guardrails

This build uses the local `_type: text_guardrails` middleware. It feeds only assistant `delta.content` into regex and Presidio, re-wraps sanitized output as NAT `ChatResponseChunk` objects, and patches the Guardrails 0.21 Presidio action to accept streaming dispatcher metadata. See `docs/README-TEXT-AWARE-GUARDRAILS.md`.

## Unified MLflow observability

This build produces one canonical NAT trace per request. The root span contains
the readable question and reconstructed final answer; NAT keeps the MCP/tool
hierarchy, and NeMo Guardrails OpenTelemetry spans inherit the same trace ID and
root span ID. The previous custom `alerts-agent.invoke` OpenTelemetry root span
has been removed, so one call no longer creates two MLflow rows.

The pinned NAT 1.8 runner is patched at image-build time by
`agent/patch_nat_single_trace.py`. Existing MLflow rows are not rewritten; only
new calls use the unified shape.

See [`docs/README-MLFLOW-OBSERVABILITY.md`](docs/README-MLFLOW-OBSERVABILITY.md) for the
architecture, rebuild steps, verification, privacy notes, and trace acceptance
criteria.

See [`docs/README-GUARDRAILS-TRACING.md`](docs/README-GUARDRAILS-TRACING.md) for the
explicit verdict attributes, self-check prompt capture, and privacy controls.

## Explicit Guardrails decision traces

The custom `text_guardrails` middleware now emits two readable child spans in
NAT's canonical trace:

- `guardrail.input.self_check` records the rendered `self_check_input` prompt,
  the actual LLM call log, activated rails, and a `passed`, `modified`, or
  `blocked` verdict.
- `guardrail.output.regex_presidio` records the overall output verdict plus
  separate `guardrail.regex.outcome` and `guardrail.presidio.outcome` values.

Open a trace in MLflow and select these spans under **Details & Timeline**. The
self-check span's Inputs/Outputs show the rendered guard prompt, raw guard-model
answer when available, activated rail log, and final decision. The output span
shows deterministic rail outcomes and the sanitized output.

Content settings:

```env
GUARDRAILS_TRACE_CAPTURE_CONTENT=true
GUARDRAILS_TRACE_CAPTURE_RAW_OUTPUT=false
GUARDRAILS_TRACE_MAX_CHARS=16384
```

`GUARDRAILS_TRACE_CAPTURE_RAW_OUTPUT` is deliberately false. Enabling it would
store text before regex blocking and Presidio masking, potentially defeating the
privacy purpose of those output rails.

## Live MLflow evaluation

The project now includes persistent MLflow datasets and on-demand evaluation
runs for Guardrails and tool calling. Every case calls the running NAT agent;
no responses or trajectories are precomputed.

```bash
docker compose --profile evaluation run --rm evaluator \
  python -m evaluation run --suite all
```

See [README-EVALUATION.md](docs/README-EVALUATION.md) for dataset management, scorer
definitions, CI behavior, and individual suite commands.
