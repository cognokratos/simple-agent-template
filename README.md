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
```

The agent keeps NeMo Guardrails as input/output middleware and uses Ollama
through its OpenAI-compatible endpoint.

## Demonstrated scenarios

The seeded database contains two open alerts (`ALT-1001`, `ALT-1002`) and one
closed alert (`ALT-1003`). Try these prompts in the browser:

1. **Show me my open alerts**
   - ReAct calls `search_alerts` with `status="open"`.
2. **Tell me more about alert ALT-1001**
   - ReAct calls `get_alert` with `alert_id="ALT-1001"`.
3. **Show all transactions for all open alerts**
   - ReAct first calls `search_alerts` with `status="open"`.
   - It then calls `get_alert` once for every returned alert ID.

The tool is named `get_alert` (singular), matching the MCP contract.

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
- request correlation, OpenTelemetry, and audit logging;
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
