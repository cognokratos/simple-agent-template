# Follow one request

> *"Show me the complete details and history for ticket TKT-1001."*

This walkthrough traces that prompt from the browser to PostgreSQL and back,
through the actual code, and ends at the trace it leaves in MLflow. Read it with
the source open: code → explanation → runtime behaviour → trace.

The agent is the Rig + Rust service in [`agent/`](../../agent). Every timing
and span name below comes from a real run against this repository on the
default configuration (`qwen3:8b` on a local Ollama, guard model the same).
Your numbers will differ. The shape should not.

## The whole path

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant UI as assistant-ui<br/>(Next.js server)
    participant GW as Gateway (Rust)
    participant MW as Agent API (Axum)
    participant GR as Input / output policy
    participant RT as Rig loop + tool policy
    participant LLM
    participant MCP as MCP server (Rust)
    participant DB as PostgreSQL
    participant OT as Collector → MLflow

    B->>UI: POST /api/gateway/chat (cookies)
    UI->>GW: POST /api/chat (cookie, x-csrf-token)
    GW->>GW: session, CSRF, schema, stream slot
    GW->>MW: POST /v1/workflow/full<br/>Bearer AGENT_API_KEY + x-authenticated-*
    MW->>MW: service key, TrustedCaller, root span
    MW->>GR: input rail
    GR->>LLM: self-check classifier (guard model)
    LLM-->>GR: "No" (do not block)
    GR->>RT: allowed
    RT->>LLM: prompt + tools + question
    LLM-->>RT: tool_call get_ticket(TKT-1001)
    RT->>RT: ToolPolicy::decide → allow
    RT->>MCP: tools/call (Bearer MCP_API_KEY)
    MCP->>DB: 2 parameterized SELECTs
    DB-->>MCP: rows
    MCP-->>RT: JSON result
    RT->>LLM: + tool result
    LLM-->>RT: answer tokens
    RT->>GR: output policy (secret patterns, PII mask), streaming
    GR-->>GW: SSE: intermediate_data + data
    GW-->>UI: SSE (streamed through)
    UI-->>B: UI message stream (tool card + Markdown)
    MW-->>OT: spans (OTLP/HTTP), one trace
```

---

## 1. The browser sends the message

The page is a client component built on assistant-ui.
[`ui/app/page.tsx`](../../ui/app/page.tsx) wires the chat runtime to a
*same-origin* route:

```tsx
() => new AssistantChatTransport({ api: "/api/gateway/chat" }),
```

The browser holds **no tokens**: no OIDC token, no API key, nothing the model
could leak. It holds an opaque, `HttpOnly` session cookie and a CSRF cookie,
both scoped to `Path=/api/gateway` ([SECURITY.md — cookies](../SECURITY.md#cookies)).

## 2. The UI server proxies to the gateway

[`ui/app/api/gateway/chat/route.ts`](../../ui/app/api/gateway/chat/route.ts)
(`POST`) runs on the Next.js server. It flattens the UI messages to
`{role, content}` text and makes a server-side `fetch` to the gateway's
`/api/chat`. It forwards the cookie header and copies the CSRF cookie into
`x-csrf-token`.

The UI is a proxy, **not a trust boundary**
([ARCHITECTURE.md](../ARCHITECTURE.md#what-each-boundary-is-for)). Everything
that matters is checked again by the gateway.

## 3. The gateway authenticates the session

`proxy::chat` in [`gateway/src/proxy.rs`](../../gateway/src/proxy.rs):

```rust
let (_session_id, session) = authenticated_session(&state, &headers).await?;
verify_csrf(&state.config, &headers, &session)?;
```

`authenticated_session` ([`auth.rs`](../../gateway/src/auth.rs)) looks up the
opaque session id, rejects expired sessions, and refreshes Keycloak tokens
(re-reading roles from `userinfo`) when the 5-minute access token is near
expiry. `verify_csrf` ([`session.rs`](../../gateway/src/session.rs)) requires the
cookie, the header and the session's own token to agree.

**Concept:** authentication is established once, by a deterministic component,
before any model is involved. → [Concept 7](../concepts/07-security-and-trust-boundaries.md)

## 4. The gateway validates, sanitises and mints identity

Still in `proxy::chat`:

1. **Schema.** The body is parsed into `ChatProxyRequest` with
   `deny_unknown_fields`. `validate_chat_request` accepts only `user` and
   `assistant` roles, requires the last message to be `user`, and bounds count,
   per-message characters and total characters.
2. **Concurrency.** A per-session stream slot (`GATEWAY_MAX_STREAMS_PER_SESSION`,
   default 4).
3. **Re-serialisation.** Anything beyond the schema does not survive.
4. **A fresh upstream request** with `Authorization: Bearer <AGENT_API_KEY>`, a
   new `x-request-id`, and identity headers built from the *session*:

```rust
let request = identity_headers(request, &session.user, true)?;
```

`identity_headers` sets `x-authenticated-user-id`, `-username`, `-roles` and
`-email`. No browser header is forwarded, so neither the page nor the model can
choose who the agent thinks the user is.

The URL is `AGENT_WORKFLOW_URL` (`http://agent:8000/v1/workflow/full`) with
`filter_steps=TOOL_START,TOOL_END,FUNCTION_START,FUNCTION_END`, so the stream
carries tool events the UI can render.

## 5. The agent authenticates its caller

The agent's HTTP surface is an Axum router
([`agent/src/api/mod.rs`](../../agent/src/api/mod.rs)). Every route except
liveness and readiness sits behind one middleware,
`require_gateway` in [`api/auth.rs`](../../agent/src/api/auth.rs):

1. the bearer token is compared with `AGENT_API_KEY` in constant time, and the
   `Authorization` header is **removed** before any handler, log or span can
   see it;
2. exactly one non-empty `x-authenticated-user-id` is required (repeated or
   control-character values are refused), plus at most one `x-request-id`;
3. only then is a [`TrustedCaller`](../../agent/src/identity.rs) built and
   attached to the request — a type with no public constructor, so no other
   code path can produce one.

The handler, `workflow_full` in [`api/workflow.rs`](../../agent/src/api/workflow.rs),
parses the body with `deny_unknown_fields`, accepts only `user` and `assistant`
roles, requires the last turn to be the user's, and keeps the last
`workflow.max_history` (20) messages. It opens the root span,
`support-tickets-agent.invoke`, spawns the run, and returns a stream over the
channel the run writes into. If the client goes away, the run is aborted.

## 6. Input guardrails run

[`agent/input_rail.rs`](../../agent/src/agent/input_rail.rs) runs the rules
held as pure functions in [`guardrails/input.rs`](../../agent/src/guardrails/input.rs):

1. refuses (does not truncate) anything over `GUARDRAILS_INPUT_MAX_CHARS`;
2. runs the critical deny patterns over the latest turn and any client-supplied
   assistant turns, and the anchored read-only allow templates;
3. makes one call to the **guard model** with the `self_check_input` prompt
   from `guardrails.prompts` in [`agent/config.yml`](../../agent/config.yml).
   No reply, or an unreadable one, counts as a block: the rail fails closed;
4. resolves the decision deterministically: a deny pattern always blocks, an
   allow template can overrule a classifier false positive, otherwise the
   classifier's verdict stands.

For this prompt the classifier said `No` and the `specific_ticket_details`
allow template also matched. The `guardrail.input.self_check` span recorded
`guardrail.decision_source = llm_and_deterministic_allow`. This step took
**2.31 s**, almost all of it the guard-model call.

**Concept:** a probabilistic check backed by deterministic ones, with the
decision source recorded. → [Concept 4](../concepts/04-guardrails-and-deterministic-controls.md)

## 7. The agent runtime invokes the LLM

`build_agent` in [`builder.rs`](../../agent/src/agent/builder.rs) builds a Rig
agent for this request: the agent model, the system prompt (its `{tools}` and
`{tool_names}` placeholders filled at startup from MCP discovery), and one tool
per allow-listed MCP tool, each closing over this request's scope. The tool
schemas go to the model as structured definitions (native tool calling).

`run` in [`execution.rs`](../../agent/src/agent/execution.rs) drives it. The
loop is bounded twice: Rig allows at most `max_tool_calls + 1` model calls, and
the tool-policy hook counts tool calls, so `max_tool_calls: 20` means exactly
twenty.

## 8. The model chooses a tool; software decides

The model returns a structured tool call, not prose (**2.71 s**):

```text
get_ticket  {"ticket_id": "TKT-1001"}
```

It chose this from the tool description (`tools.mcp.overrides` in
`agent/config.yml`) and the system-prompt rule *"For details about a specific
ticket, call get_ticket using its exact ID."* This is the probabilistic decision
in the request.

Before Rig runs the call it invokes `ToolPolicyHook::on_dispatch`
([`hooks.rs`](../../agent/src/agent/hooks.rs)), which checks the tool budget and
asks [`ToolPolicy::decide`](../../agent/src/guardrails/tools.rs): a known tool,
a JSON object, only declared fields of the declared types, read-only — `Allow`,
in under a millisecond, recorded as a `tool.policy` span. A `Deny` would skip
the tool and hand the model the reason as the tool's result. Everything after
this point is deterministic again until the answer is written.

## 9. The MCP request is issued

The executor, `execute` in [`mcp/tools.rs`](../../agent/src/mcp/tools.rs),
re-checks the call against the same policy — an executor must not depend on a
hook configured elsewhere — and then calls `tools/call` through `rig-rmcp` over
streamable HTTP to `http://mcp-server:8080/mcp`, with
`Authorization: Bearer ${MCP_API_KEY}` and a 30-second
`tools.mcp.tool_call_timeout_seconds`. In the stream, this appears as:

```text
intermediate_data: {"type":"TOOL_START","name":"tickets_mcp__get_ticket", ... "input":{"ticket_id":"TKT-1001"} ...}
```

## 10. The Rust MCP server validates and executes

[`mcp-server/src/main.rs`](../../mcp-server/src/main.rs):

* `require_api_key` compares the bearer token in constant time and **removes** it
  before RMCP logging;
* RMCP deserialises arguments into `GetTicketArgs { ticket_id: String }`, a typed
  schema;
* `get_ticket` trims and rejects an empty id, then runs two parameterized
  queries: the ticket by `id = $1`, and its `ticket_events` by
  `ticket_id = $1 ORDER BY occurred_at DESC`.

## 11. PostgreSQL provides authoritative data

The rows come from [`db/init.sql`](../../db/init.sql): `TKT-1001`, "Delayed
delivery", `open`, `medium`, customer Renee Castillo, assigned to Priya Shah,
with three history events `EVT-1001`–`EVT-1003`. `CHECK` constraints guarantee
`status` and `priority` are valid values. PostgreSQL is reachable from the MCP
server and nothing else (`data_net`).

## 12. The tool result returns to the agent

`get_ticket` returns one text content block containing JSON:

```json
{"ticket": {"id": "TKT-1001", "subject": "Delayed delivery", "status": "open", "priority": "medium", ...},
 "history_count": 3,
 "history": [{"id": "EVT-1003", "event_type": "support_note", ...}, ...]}
```

The tool call took **0.05 s**. The `TOOL_END` event and the trace carry a copy
of the result with secrets redacted and PII masked; the model gets the raw one.
From here the result is part of the conversation,
and every string in it is **untrusted data**: a description or support note
could contain instructions. → [Lab 04](04-break-the-agent.md#experiment-1-indirect-prompt-injection)

## 13. The LLM writes the final answer

Rig sends the conversation plus the tool result back to the model, which writes
the answer (**10.53 s**). `run` in `execution.rs` reads Rig's stream and keeps
only assistant text fragments as candidates for the client; reasoning fragments
and tool-call arguments are never streamed.

## 14. Output guardrails run

Each fragment passes the `StreamingOutputGuard` in
[`guardrails/output.rs`](../../agent/src/guardrails/output.rs). Text is released
only once 320 more characters have arrived behind it; the held text plus 512
characters of look-behind is scanned for the credential and prompt-leakage
patterns in `guardrails.output.secret_patterns`, and PII from
`guardrails.output.pii_entities` is masked by the deterministic recognisers in
[`guardrails/pii.rs`](../../agent/src/guardrails/pii.rs) before release. A
secret match blocks the rest of the answer. The released text is what gets
recorded as the trace's answer.

Outcome `passed`; the client saw 308 fragments as they were released, not one
buffered block. Names stay visible on purpose: `PERSON` and `ORGANIZATION` are
not in the entity list.

## 15. The result streams back

The agent's response is server-sent events. Abridged:

```text
intermediate_data: {"type":"WORKFLOW_START","name":"support-tickets-agent.invoke", ...}
intermediate_data: {"type":"FUNCTION_START","name":"guardrail_input_self_check_decision", ...}
intermediate_data: {"type":"FUNCTION_END","name":"guardrail_input_self_check_decision", ...}
intermediate_data: {"type":"TOOL_START","name":"tickets_mcp__get_ticket", ...}
intermediate_data: {"type":"TOOL_END","name":"tickets_mcp__get_ticket", ...}
data: {"value": "The complete details and history for ticket **TKT-1001** ..."}
data: {"value": "..."}
intermediate_data: {"type":"FUNCTION_END","name":"guardrail_output_regex_pii_decision", ...}
intermediate_data: {"type":"WORKFLOW_END","name":"support-tickets-agent.invoke", ...}
```

The workflow events carry `agent_runtime: "rig-rust"` in their metadata. The
gateway asks only for `TOOL_START,TOOL_END,FUNCTION_START,FUNCTION_END`
(`AGENT_FILTER_STEPS`), so the `WORKFLOW_*` lines are filtered out on the browser
path; the evaluator receives them.

The gateway streams it through without buffering (`x-accel-buffering: no`, no
request timeout on this route). The UI route parses `intermediate_data:` into
tool cards (`tool-input-available` / `tool-output-available`) and `data:` into
answer text, using the wire helpers in [`ui/lib/agent-wire.ts`](../../ui/lib/agent-wire.ts).
The page renders Markdown with `react-markdown` **without** raw HTML, so a model
answer cannot inject raw HTML.

## 16. OpenTelemetry records the execution

[`agent/src/telemetry/`](../../agent/src/telemetry/mod.rs) exports spans over
OTLP/HTTP to the collector. Every span of the request is created while the root
span is current, so the input rail and its classifier call, Rig's own `chat`
and `execute_tool` spans, each tool-policy decision, the MCP call and the output
rail all land in one trace. An inbound W3C `traceparent` is honoured.

What never reaches a span: the service credentials (stripped before any handler
runs), the raw user subject (only a pseudonym, and only with
`OTEL_TRACE_USER_ID=true`) and, by default, raw model text before the output
rail. The collector
([`observability/otel-collector.yml`](../../observability/otel-collector.yml))
batches and forwards to MLflow's `/v1/traces`, experiment `0` (Default).

## 17. MLflow lets you inspect it

The trace recorded for this run:

```text
support-tickets-agent.invoke                        15.59 s
  guardrail.input.self_check   llm_and_deterministic_allow   2.31 s
    guard_model.call → chat  qwen3:8b                2.31 s
  chat  qwen3:8b             (chooses get_ticket)    2.71 s
  execute_tool                                       0.05 s
    tool.policy              allow                  <0.01 s
    tickets_mcp__get_ticket  ok                      0.05 s
  chat  qwen3:8b             (writes the answer)    10.53 s
  guardrail.output.stream    passed                 13.26 s  (overlaps the answer)
```

Request preview: the question. Response preview: the released answer. The
latency budget:

| | This run |
| --- | --- |
| Total (`support-tickets-agent.invoke`) | 15.59 s |
| Input rail (guard-model call) | 2.31 s |
| Agent model, choosing the tool | 2.71 s |
| Tool policy | < 0.01 s |
| `get_ticket` (MCP + PostgreSQL) | 0.05 s |
| Agent model, writing the answer | 10.53 s |

The output rail's span overlaps the answer, because it runs on the stream. The
agent model's calls are their own spans, so the split is read directly: almost
all of the request was model time. Agent latency was model latency.

Open it yourself: `make open-mlflow` → **Experiments → Default → Traces**. See
[lab 06](06-debug-with-traces.md).

Steps 1–4 and 9–12 were the same code for the retired NeMo Agent Toolkit agent
(`nat-1.9-final`), and it streamed the same SSE vocabulary; what differed inside
the agent box, and how the two runs compared, is in [FRAMEWORK-MIGRATION.md](../FRAMEWORK-MIGRATION.md#what-was-measured).
The Rust internals are taught step by step in [AGENT-INTERNALS.md](../AGENT-INTERNALS.md).

## What to take away

| Step | Probabilistic or deterministic | Enforced by |
| --- | --- | --- |
| 1–5 authentication, identity, validation | deterministic | Keycloak, gateway, agent API (`require_gateway`) |
| 6 input classification | **both** | guard model + patterns + templates |
| 7–8 tool choice | **probabilistic** | the model |
| 8 tool-call decision | deterministic | `ToolPolicy::decide` at Rig's dispatch hook |
| 9–12 tool execution, data | deterministic | MCP server, SQL, PostgreSQL |
| 13 answer | **probabilistic** | the model |
| 14 output controls | deterministic | secret patterns, PII recognisers |
| 15–17 delivery, tracing | deterministic | gateway, UI, OTel |

The model made two decisions in this request: which tool to call, and what to
say. Everything else was ordinary software.

## Go deeper

* [Learning path](../LEARNING-PATH.md) · [ARCHITECTURE.md](../ARCHITECTURE.md) ·
  [SECURITY.md](../SECURITY.md) · [OBSERVABILITY.md](../OBSERVABILITY.md)
* Same prompt as an evaluation case: `TOOLS-GET-TICKET-1001` in
  [`evaluation/datasets/tool_calling.json`](../../evaluation/datasets/tool_calling.json)
  and `GR-ALLOW-TICKET-DETAILS` in
  [`evaluation/datasets/guardrails.json`](../../evaluation/datasets/guardrails.json)
