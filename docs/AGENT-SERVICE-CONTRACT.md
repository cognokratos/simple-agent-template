# The agent-service contract

The stable boundary any agent implementation must satisfy. It sits between the
gateway (and the evaluator) on one side and *an* agent on the other. Because the
agent honours it, the UI, the gateway, the evaluator and the MCP server do not
depend on which framework runs the agent loop — which is what let the runtime be
replaced as a single variable ([FRAMEWORK-MIGRATION.md](FRAMEWORK-MIGRATION.md)).

The Rust agent in [`agent/`](../agent/) implements it
([`agent/src/api/`](../agent/src/api/)). The retired NeMo Agent Toolkit agent
(`nat-1.9-final`) was the first implementation to satisfy it; the vocabulary
below is inherited from that runtime unchanged.

The contract was recovered from what the consumers actually parse
(`gateway/src/proxy.rs`, `ui/app/api/gateway/chat/route.ts`,
`evaluation/client.py`), not from any framework's documentation; see
[NAT-AGENT-ANALYSIS.md](NAT-AGENT-ANALYSIS.md#2-the-gateway--agent-contract-as-consumed).
It is asserted offline by [`agent/tests/contract.rs`](../agent/tests/contract.rs)
and against a live cluster by `make auth-test`.

## Routes

| Route | Authentication | Purpose |
| --- | --- | --- |
| `GET /health`, `GET /health/live` | none | liveness |
| `GET /health/ready` | none | readiness: the MCP session is open |
| `GET /version` | key + identity | provenance for evaluation |
| `POST /v1/workflow/full` | key + identity | the streaming workflow |
| `POST /executions/{execution_id}/interactions/{interaction_id}/response` | key + identity | a human's answer to an approval prompt — **only mounted when approvals are enabled** |

Every other path answers `401` without the service key and `404` with it. An
implementation should serve nothing beyond these routes; the retired NAT agent
also exposed routes nothing in this repository called (`/v1/workflow`,
`/v1/chat`, OpenAI-compatible routes, `/docs`).

## Authentication, in this order

1. `Authorization: Bearer <AGENT_API_KEY>`, compared in constant time, then
   removed from the request before anything else sees it. Missing or wrong → `401`,
   `{"error":"invalid or missing internal API key"}`, `WWW-Authenticate: Bearer`.
2. Exactly one non-empty `x-authenticated-user-id`. Missing, empty or repeated
   → `401`, `{"error":"missing or ambiguous authenticated identity"}`.

The key answers "is this the gateway"; the header answers "who is it acting
for". Network reachability answers neither.

## Trusted request headers

| Header | Sent by | Meaning |
| --- | --- | --- |
| `x-authenticated-user-id` | gateway, evaluator (`evaluation-harness`) | the authenticated subject; the approval token's `actor_id` |
| `x-authenticated-username`, `-roles`, `-email` | gateway | profile; roles are comma-separated |
| `x-request-id` | gateway (fresh UUID per request) | correlation; the approval token's `request_id` |
| `traceparent` | optional | W3C trace context to join |

Values are percent-encoded by the gateway when not printable ASCII. A header
the browser sent never reaches the agent: the gateway builds a fresh request.
The agent never binds an approval to a request without a gateway-minted
`x-request-id` — only that id names an authenticated request the MCP server can
check a token against. Without one it generates an id for tracing (the evaluator
sends none) but refuses to bind an approval to it. A *repeated* or malformed
`x-request-id` is `400`.

## `POST /v1/workflow/full`

Query: `filter_steps` (comma-separated step types to stream; absent = all).

Body (unknown fields refused):

```json
{"messages": [{"role": "user" | "assistant", "content": "…"}],
 "stream": true, "user": "…", "evaluation_case_id": "…"}
```

`messages` must be non-empty, end with a `user` turn, contain no empty
message; only `user` and `assistant` roles exist. The last 20 messages are kept,
starting on a user turn (`max_history`). The three optional fields are what the
evaluator sends; they are accepted and ignored.

Response: `200`, `text/event-stream`, `x-request-id` echoed. Events:

```text
intermediate_data: {"id","parent_id","type","name","payload"}
data: {"value": "<released answer text>"}
event: interaction_required
data: {"execution_id","interaction_id","prompt":{…}}
{"error": "workflow_error", "message": "…"}
```

| Event | Rules |
| --- | --- |
| step | `type` ∈ `WORKFLOW_START`, `WORKFLOW_END`, `TOOL_START`, `TOOL_END`, `FUNCTION_START`, `FUNCTION_END`. Tool steps are named `<function_group>__<tool>` (`tickets_mcp__get_ticket`). Arguments in `payload.data.input` and `payload.metadata.tool_inputs`; results in `payload.data.output`. Workflow steps carry `payload.metadata.provided_metadata.workflow_trace_id`. |
| answer | `value` is always a string, even when it looks like a number; consumers parse only JSON *containers* inside it. Only text that passed the output policy is ever sent. |
| interaction | `prompt.input_type` is `radio` (with `options[]` of `{id,label,value,description}`) or `text` (with `placeholder`, `required`). A cancel option is always offered: `id: "cancel"`, `value: "__CANCEL__"`. |
| error | `{"error":"workflow_error","message":…}` as a bare JSON block, not an SSE field (the shape inherited from NAT); both consumers detect it by its leading `{`. The message is a class (`The model provider failed to answer.`, `The agent run failed.`), never a provider's own text. |

### Decision events

Function steps named `guardrail_input_*_decision` and `guardrail_output_*_decision`
carry the policy decisions in `payload.data.output`. The evaluator scores
`blocked` from the input decision — it never infers a block from wording.

| Rail | Event |
| --- | --- |
| input | `guardrail_input_self_check_decision` |
| output | `guardrail_output_regex_pii_decision` |

The evaluator matches each by its `guardrail_input_` / `guardrail_output_`
prefix, so the retired NAT agent's `guardrail_output_regex_presidio_decision`
still scores.

## `POST /executions/{e}/interactions/{i}/response`

Both ids must be UUIDs (`400`). Body ≤ 16 KiB, unknown fields refused:

```json
{"response": {"type": "radio", "selected_option": {"id","label","value","description"}}}
{"response": {"type": "text", "text": "…"}}
{"response": {"type": "binary_choice", "selected_option": {"id","label","value": true}}}
```

Before a suspended run resumes, the agent checks that the interaction exists
and is still pending, the responder is the user the prompt was addressed to, the
response is the kind of answer the prompt asked for, and the `(id, value)` pair
is one the prompt actually offered (a cancellation only when self-consistent).
These checks are part of the interaction registry
([`agent/src/approval/pending.rs`](../agent/src/approval/pending.rs)); a refused
answer leaves the prompt pending for its owner.

| Outcome | Status |
| --- | --- |
| accepted; the suspended run resumes | `204` |
| no such pending interaction (never created, answered, abandoned or expired) | `404` |
| not the owner (the prompt stays pending) | `403` |
| wrong kind, unoffered pair, half-cancel, empty rationale | `422` |
| the run has gone | `410` |

An implementation that authorises a response on knowledge of the two UUIDs alone
does not satisfy the contract — stock NAT did, which is why the retired agent
had to add these checks.

## `GET /version`

Provenance, digests only — never prompt text or a credential. Fields:
`available`, `agent_runtime`, `agent_framework`, `agent_framework_version`,
`mcp_sdk`, `mcp_sdk_version`, `agent_version`, `model`, `guard_model`,
`reasoning_effort`, `guard_reasoning_effort`, `build_commit`, `config_path`,
`config_sha256`, `prompt_sha256`, `guardrails_prompts_sha256`,
`guardrails_prompts_digest: "canonical-json"` and `tools_exposed`
([`telemetry/provenance.rs`](../agent/src/telemetry/provenance.rs)).

The Rust agent reports `agent_runtime: "rig-rust"`, `agent_framework: "rig"`,
`mcp_sdk: "rmcp"`. The evaluator records the runtime as
`provenance.agent.runtime`, and records `nat` for a reachable agent that does
not report the field (the retired NAT agent predates it).

## Error classes

| Class | Status | Body |
| --- | --- | --- |
| not the gateway | 401 | `{"error":"invalid or missing internal API key"}` |
| no identity | 401 | `{"error":"missing or ambiguous authenticated identity"}` |
| invalid request | 400, naming the field. The gateway refuses malformed browser requests before the agent sees them. | `{"error":"…"}`, never echoing a credential |
| not the owner | 403 | `{"error":"…"}` |
| no such route / interaction | 404 | `{"error":"…"}` |
| response not offered | 422 | `{"error":"…"}` |
| run gone | 410 | `{"error":"…"}` |
| workflow failure after the stream started | 200 + error block | see above |

## The agent → MCP side

Independent of the agent framework:

* MCP over streamable HTTP at `TICKETS_MCP_URL`, `Authorization: Bearer
  $MCP_API_KEY`; only `search_tickets` and `get_ticket` are exposed.
* Read-only calls carry **no user identity**: the MCP boundary authenticates the
  agent, not the person.
* With approvals enabled, `POST {mcp}/approvals/execute`
  `{"approval_token","request_id"}`; the token format is defined in
  [APPROVALS.md](APPROVALS.md#the-token) and verified by
  `mcp-server/src/approval.rs`.
