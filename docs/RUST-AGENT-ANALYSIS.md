# Mapping the NAT agent's responsibilities

What the NeMo Agent Toolkit (NAT) agent on `main` actually does, traced through
its code rather than inferred from file names. It was written **before** the Rig
implementation on `rust-agent`, as the parity checklist that implementation is
held to, and it is kept because it is also the most compact answer to *"what is
an agent service responsible for?"* — a useful exercise for any runtime. Where
the Rig agent deliberately differs, the difference is in
[NAT-VS-RIG.md](NAT-VS-RIG.md) and [LIMITATIONS.md](LIMITATIONS.md), not here.

Base: `main` at `8ed2ceb` (NAT 1.9, NeMo Guardrails 0.21, Rig 0.44 evaluated).

## 1. What the agent is responsible for

Traced from `agent/config.yml`, `agent/src/nat_streaming_react/*` and the two
callers that exist: the gateway (`gateway/src/proxy.rs`) and the evaluator
(`evaluation/client.py`).

| # | Responsibility | Where on `main` | Deterministic? |
| --- | --- | --- | --- |
| R1 | Authenticate the caller as the gateway (static bearer key, constant time, stripped before anything else sees it) | `fastapi_worker.StaticServiceKeyMiddleware` | yes |
| R2 | Require exactly one non-empty `x-authenticated-user-id`; 401 otherwise, *after* the key check | `fastapi_worker.RequireIdentityHeaderMiddleware` | yes |
| R3 | Liveness only on unauthenticated paths: `/health`, `/health/live`, `/health/ready` | `PUBLIC_PATHS` | yes |
| R4 | `GET /version` provenance (build commit, prompt/config digests, model names, exposed tools; never a credential or prompt text), authenticated | `provenance.py` | yes |
| R5 | `POST /v1/workflow/full` streaming SSE: `intermediate_data:` step events, `data: {"value": …}` answer chunks, `event: interaction_required`, plain-JSON error blocks | NAT front end + `register.py` | yes |
| R6 | `filter_steps` query parameter selects which step types are streamed | NAT front end | yes |
| R7 | Trim history to the last 20 messages, starting on a user turn | `register.py` (`trim_messages`, `max_history: 20`) | yes |
| R8 | Input rail: refuse (not truncate) a latest user turn over 32 000 chars | `text_guardrails.pre_invoke` | yes |
| R9 | Input rail: LLM self-check with the `self_check_input` prompt; verdict parsed fail-closed (`is_content_safe`: first two tokens, unknown ⇒ unsafe) | NeMo Guardrails | **probabilistic** classifier, deterministic parser |
| R10 | Input rail: critical deny patterns over the latest turn **and** client-supplied assistant turns (not prior user turns) | `_CRITICAL_INPUT_PATTERNS`, `_prior_turn_text` | yes |
| R11 | Input rail: anchored read-only allow templates may override an LLM false positive, never a deterministic block | `_READ_ONLY_TICKET_TEMPLATES`, `_resolve_input_policy` | yes |
| R12 | Emit `guardrail_input_self_check_decision` as a `FUNCTION_END` step so the evaluator scores the exact decision | `_emit_nat_evaluation_event` | yes |
| R13 | ReAct loop, native tool calling, bounded by `max_tool_calls: 20`; tool errors passed back to the model | NAT/LangGraph `ReActAgentGraph` | the loop is deterministic, every decision in it is the model's |
| R14 | Discover MCP tools over streamable HTTP with `Authorization: Bearer $MCP_API_KEY`; expose only `search_tickets` and `get_ticket`; override their descriptions; 30 s call timeout; reconnect | NAT `mcp_client` function group | yes |
| R15 | Stream answer tokens as they arrive (native tool calling: no `Final Answer:` marker) | `register._stream_fn` | yes |
| R16 | Output rail: regex block of credential / prompt-leakage patterns over a rolling window, so a secret split across chunks is still caught | NeMo `regex check output` + `guardrails_compat` | yes |
| R17 | Output rail: Presidio PII masking (`EMAIL_ADDRESS`, `PHONE_NUMBER`, `CREDIT_CARD`, `IBAN_CODE`, `IP_ADDRESS`, `CRYPTO`, `US_SSN`, replaced by `<ENTITY_TYPE>`). Because NeMo cannot rewrite streamed text, **the whole answer is buffered** before release; over 200 000 chars it is refused | `_stream_with_buffered_masking` | NER is model-based (spaCy), masking is deterministic |
| R18 | Emit `guardrail_output_regex_presidio_decision` | `text_guardrails` | yes |
| R19 | One OpenTelemetry trace per request: workflow root, LLM and tool spans, guardrail spans; readable question/answer on the root; credential headers redacted; raw identity headers redacted; per-user id only when `OTEL_TRACE_USER_ID=true`; honour inbound `traceparent` | `observability/*` | yes |
| R20 | Optional, off-by-default human approval (see §4) | `approval.py`, `interaction_guard.py` | yes, around one model proposal |

Not the agent's responsibility, and must stay that way: OIDC, sessions, CSRF,
browser input schema, per-session stream limits (gateway); SQL, the capability
set, nonce consumption, row locks, transition policy, audit (MCP server).

## 2. The gateway ↔ agent contract (as consumed)

**Gateway → agent, chat** (`proxy::chat`): `POST {AGENT_WORKFLOW_URL}?filter_steps=TOOL_START,TOOL_END,FUNCTION_START,FUNCTION_END`,
headers `Authorization: Bearer <AGENT_API_KEY>`, `Accept: text/event-stream`,
fresh `x-request-id` (UUID), `x-authenticated-user-id`, `-username`, `-roles`
(comma-separated), `-email`; body is the re-serialised
`{"messages":[{"role","content"}]}` (roles `user|assistant`, last is `user`,
bounded). No timeout: the response is a long-lived stream relayed byte for byte.
A non-2xx answer becomes `{"error":"the agent rejected the request"}`.

**Gateway → agent, interaction**: `POST /executions/{e}/interactions/{i}/response`
(both UUIDs), headers as above minus email, 10 s timeout, body
`{"response": {"type": "radio"|"text"|"binary_choice", …}}` after structural
validation (bounded fields; a cancellation must carry both `id: "cancel"` and
`value: "__CANCEL__"`). Status and body are relayed; the UI treats any 2xx as
accepted (204 included).

**Evaluator → agent**: same workflow route, adds `WORKFLOW_START,WORKFLOW_END`
to `filter_steps`, sends `x-authenticated-user-id: evaluation-harness`, **no
`x-request-id`**, and a body with extra fields `stream`, `user`,
`evaluation_case_id`. Reads `GET /version`.

**Wire vocabulary the two consumers actually parse** (`ui/app/api/gateway/chat/route.ts`,
`evaluation/client.py`):

* `intermediate_data: {"id","parent_id","type","name","payload"}` — `type`
  `TOOL_START|TOOL_END` for tools, `FUNCTION_END` for guardrail decisions
  (`payload.data.output`); tool input from `payload.metadata.tool_inputs` or
  `payload.data.input`, output from `payload.data.output`; trace id from any
  `workflow_trace_id` key;
* `data: {"value": "<text>"}` — answer text, never re-parsed when scalar;
* `event: interaction_required` + `data: {"execution_id","interaction_id","prompt":{"input_type","text","options":[{id,label,value,description}],"placeholder","required"}}`;
* a block that starts with `{` is a workflow error (`message`/`details`).

## 3. Identity

Identity reaches the agent only as gateway-minted headers on a freshly built
request. On `main` it is used for exactly three things: refusing an anonymous
request (R2), binding the approval owner and responder (§4), and the
`actor_id` claim in the approval token, which the MCP server writes to
`ticket_audit`. **Read-only MCP calls carry no user identity** — the MCP
boundary authenticates the agent by service key only. The model never sees an
identity, and no tool argument can carry one.

## 4. The approval lifecycle on `main`

1. The model calls the agent-side function `ticket_priority_change(ticket_id,
   current_priority, requested_priority, summary, note?)` — not an MCP tool.
2. The function reads actor and `x-request-id` from the request; refuses
   without both.
3. NAT pauses the workflow inside the function and streams
   `interaction_required` (radio: every priority as *Keep*/*Change to* plus
   *Cancel*). The SSE stream stays open.
4. The response route is guarded: responder == owner, response type ==
   prompt type, `(id, value)` pair offered by *this* prompt, a cancel is only a
   self-consistent cancel.
5. Cancel ⇒ "cancelled"; same priority ⇒ nothing minted.
6. Otherwise a second prompt (text) demands a reason; `__CANCEL__` cancels.
7. Mint an HMAC-SHA256 token over canonical JSON claims (`v, exp, action,
   resource_id, actor_id, request_id, choice, expected_choice,
   override_requested, rationale, payload, payload_sha256, nonce`), TTL 600 s
   (60–1800).
8. `POST {mcp}/approvals/execute {approval_token, request_id}` with the MCP key.
   The MCP server verifies signature, bindings, lifetime, re-derives state under
   a row lock, applies policy, consumes the nonce, writes audit — one
   transaction.
9. The model is told `committed` true/false; it never restates the payload.

NAT's pending-interaction store is **in memory**, and the paused workflow is a
suspended coroutine holding the open SSE stream. There is no durable
suspension on `main`.

## 5. Evaluation dependencies on the agent

The evaluator scores from the stream alone: `blocked` from the input-decision
event, tool calls from `TOOL_START`, tool results from `TOOL_END`, the answer
from `data:` lines. Provenance comes from `/version` and from reading
`agent/config.yml` (`workflow.system_prompt`, rail prompts) to compare digests.

## 6. What Rig 0.44 offers (measured from the crate source)

* `rig-agent` 0.44: `Agent`/`AgentBuilder`, a hook-aware runner
  (`agent.prompt(..).history(..).max_turns(..).add_hook(..).stream()`), and
  `AgentRun` — a **serializable, sans-I/O state machine** of the loop
  (`CallModel`/`CallTools`/`Done`) that `Agent::resume` can continue.
* `AgentHook::on_dispatch` sees every tool call (name + raw JSON arguments)
  before it runs and answers `Proceed`, `Patch` or `Deny` (skip with a reason
  the model sees, or cancel the run). `on_invalid_tool_call` decides what an
  unknown tool name does.
* `rig-rmcp` 0.44 adapts an `rmcp` 2.x tool into a Rig tool (`McpTool`,
  `execute_mcp`, `mcp_result_output`).
* `rig-core` 0.44 `OpenAIConfig::with_base_url(..).client().chat(model)` speaks
  the chat-completions wire any OpenAI-compatible endpoint (Ollama included)
  serves.
* Building an `Agent` spawns nothing, so one can be built per request with
  tools bound to that request's trusted context.

## 7. Parity requirements and design decisions for `rust-agent`

| Concern | Decision |
| --- | --- |
| Agent loop | Rig's runner owns it (as NAT/LangGraph does on `main`); a fresh `Agent` per request. |
| Tool policy | An explicit Rig `AgentHook::on_dispatch` calling a pure `ToolPolicy::decide`; executors re-validate (defense in depth). Unknown tools resolved by `on_invalid_tool_call` as a deterministic skip. |
| MCP | `rmcp` 2.2 client (same SDK version as the server), wrapped through `rig-rmcp`'s `McpTool`; strict schema validation before every call; allow-list and description overrides kept in `config.yml`. |
| Identity | Gateway headers → typed `TrustedCaller`, owned by the request; never in prompt, tool schema or tool arguments. |
| Approvals | Same tool name, schema, prompts, token format and MCP endpoint. The pending interaction is an explicit state machine owned by an `InteractionRegistry`; the run suspends inside the approval executor exactly as NAT's coroutine does; a decision value can only be constructed by the authorization check. In-memory, like `main`; `AgentRun` serializability is documented as the route to durable suspension, not implemented. |
| Input rail | Same prompt, same deterministic patterns and templates, same precedence, same parser semantics, same event. |
| Output rail | Same credential patterns. PII: deterministic recognisers (regex + checksums), applied in a bounded streaming window instead of buffering the whole answer. **Narrower than Presidio — documented, not claimed equivalent.** |
| Wire | Same routes, same SSE vocabulary, same error classes. Only the routes the two consumers use are implemented. |
| Observability | `tracing` + `tracing-opentelemetry` + OTLP/HTTP to the same collector; Rig's spans nest under the request root (measured during implementation: Rig adopts the root as its agent span, so its `chat` and `execute_tool` spans are direct children). |
| Config | Same environment variable names, including the historical `NAT_*` ones. |
| Evaluation | Datasets and scorers untouched; `/version` reports `agent_runtime: "rig-rust"`; the evaluator learns the Rust config layout for prompt digests. |

## 8. Implementation plan (as followed for `rust-agent`)

1. **Skeleton** — Cargo crate, typed config (fail fast), errors, Axum routes,
   auth/identity middleware, health, `/version`, Docker image, Compose wiring.
2. **Rig** — OpenAI-compatible model, system prompt, streaming run, SSE writer.
3. **MCP** — rmcp client, discovery, allow-list, strict schema validation,
   policy-wrapped executors, `TOOL_START/END` events.
4. **Policy** — input rail (deterministic + guard model), tool policy hook,
   streaming output rail (secrets + PII) with cross-chunk tests.
5. **Approvals** — interaction registry, response route, suspension, token
   minting, MCP execution, adversarial tests (including verifying agent-minted
   tokens with the MCP server's own verifier source).
6. **Observability** — OTLP, request/trace correlation, spans, redaction.
7. **Evaluation** — provenance, evaluator config layout, live runs.
8. **Docs and labs** — README notice, contract, NAT vs Rig, maintenance policy,
   Rust learning path, limitations.
9. **Hardening** — fmt, clippy, tests, static checks, Compose end to end, user
   journeys, approve/cancel, adversarial variants.
