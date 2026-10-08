# The agent-service contract

The stable boundary that both agent implementations satisfy: the canonical
NeMo Agent Toolkit (NAT) agent on `main`, and the Rig + Rust agent on the
`rust-agent` branch. It sits between the gateway (and the evaluator) on one
side and *an* agent on the other. Because both honour it, the UI, the gateway,
the evaluator and the MCP server are identical on the two branches — which is
what lets the agent runtime be studied as the single variable
([NAT-VS-RIG.md](NAT-VS-RIG.md)).

Where the two implementations deviate from each other, the row or paragraph is
labelled **NAT** or **Rig**. Everything unlabelled holds for both.

The contract was recovered from what the consumers actually parse
(`gateway/src/proxy.rs`, `ui/app/api/gateway/chat/route.ts`,
`evaluation/client.py`), not from NAT's documentation; see
[RUST-AGENT-ANALYSIS.md](RUST-AGENT-ANALYSIS.md#2-the-gateway--agent-contract-as-consumed).
The contract is asserted for both runtimes by `make auth-test` against a live
cluster, and for the Rig agent additionally offline by
[`agent/tests/contract.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/tests/contract.rs).

## Routes

| Route | Authentication | Purpose |
| --- | --- | --- |
| `GET /health`, `GET /health/live` | none | liveness |
| `GET /health/ready` | none | readiness (Rig: MCP session open) |
| `GET /version` | key + identity | provenance for evaluation |
| `POST /v1/workflow/full` | key + identity | the streaming workflow |
| `POST /executions/{execution_id}/interactions/{interaction_id}/response` | key + identity | a human's answer to an approval prompt — **only mounted when approvals are enabled** |

Every other path answers `401` without the service key. **NAT** additionally
serves routes nothing in this repository calls (`/v1/workflow`, `/v1/chat`,
OpenAI-compatible routes, `/docs`). **Rig** implements only the routes above, so
its authenticated surface is smaller.

## Authentication, in this order

1. `Authorization: Bearer <AGENT_API_KEY>` (the agent reads it as
   `NAT_GATEWAY_API_KEY`), compared in constant time, then removed from the
   request before anything else sees it. Missing or wrong → `401`,
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
Neither agent binds an approval to a request without a gateway-minted
`x-request-id` — only that id names an authenticated request the MCP server can
check a token against. **NAT** refuses to mint without one; **Rig** generates an
id for tracing (the evaluator sends none) but refuses to bind an approval to it,
and refuses a *repeated* `x-request-id` with `400`.

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
| error | NAT's workflow-error shape: a bare JSON block, not an SSE field; both consumers detect it by its leading `{`. **Rig** puts a class in the message (`The model provider failed to answer.`), never a provider's own text; **NAT** reports the workflow exception. |

### Decision events

Function steps named `guardrail_input_*_decision` and `guardrail_output_*_decision`
carry the policy decisions in `payload.data.output`. The evaluator scores
`blocked` from the input decision — it never infers a block from wording.

| | NAT | Rig |
| --- | --- | --- |
| input | `guardrail_input_self_check_decision` | same name, same fields |
| output | `guardrail_output_regex_presidio_decision` | `guardrail_output_regex_pii_decision` (no Presidio); the evaluator matches either by the `guardrail_output_` prefix |

## `POST /executions/{e}/interactions/{i}/response`

Both ids must be UUIDs (`400`). Body ≤ 16 KiB, unknown fields refused:

```json
{"response": {"type": "radio", "selected_option": {"id","label","value","description"}}}
{"response": {"type": "text", "text": "…"}}
{"response": {"type": "binary_choice", "selected_option": {"id","label","value": true}}}
```

Both agents enforce the same rules before a suspended run resumes: the
interaction exists and is still pending, the responder is the user the prompt
was addressed to, the response is the kind of answer the prompt asked for, and
the `(id, value)` pair is one the prompt actually offered (a cancellation only
when self-consistent).

| Outcome | NAT | Rig |
| --- | --- | --- |
| accepted; the suspended run resumes | `204` | `204` |
| refused by one of the rules above | refused by [`interaction_guard.py`](https://github.com/cognokratos/simple-agent-template/blob/main/agent/src/nat_streaming_react/interaction_guard.py) before NAT resolves it; the HTTP status is NAT's mapping of the exception and is not asserted on `main` | `404` no such pending interaction (never created, answered, abandoned or expired) · `403` not the owner (the prompt stays pending) · `422` wrong kind, unoffered pair, half-cancel, empty rationale · `410` the run has gone |

Stock NAT authorises an interaction response on knowledge of two UUIDs alone;
`main` adds the checks by substituting NAT's execution store. On `rust-agent`
they are part of the interaction registry itself
([`agent/src/approval/pending.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/approval/pending.rs)).

## `GET /version`

Provenance, digests only — never prompt text or a credential. Common fields:
`available`, `model`, `guard_model`, `reasoning_effort`,
`guard_reasoning_effort`, `build_commit`, `config_sha256`, `prompt_sha256`,
`guardrails_prompts_sha256`, `tools_exposed`. **Rig** adds
`agent_runtime: "rig-rust"`, `agent_framework`, `agent_framework_version`,
`mcp_sdk`, `mcp_sdk_version` and `guardrails_prompts_digest: "canonical-json"`.
**NAT** reports `agent_runtime: "nat"`, **Rig** `"rig-rust"`; the evaluator
records it as `provenance.agent.runtime` (and infers `nat` from an older NAT
build that does not report it). Because the system prompt is byte-identical on
both branches, `prompt_sha256` is the same for both runtimes.

## Error classes

| Class | Status | Body |
| --- | --- | --- |
| not the gateway | 401 | `{"error":"invalid or missing internal API key"}` |
| no identity | 401 | `{"error":"missing or ambiguous authenticated identity"}` |
| invalid request | Rig: 400, naming the field; NAT: FastAPI's own request validation. The gateway refuses malformed browser requests before either sees them. | `{"error":"…"}`, never echoing a credential |
| not the owner | 403 (Rig; NAT: see above) | `{"error":"…"}` |
| no such route / interaction | 404 | `{"error":"…"}` |
| response not offered | 422 (Rig; NAT: see above) | `{"error":"…"}` |
| run gone | 410 (Rig) | `{"error":"…"}` |
| workflow failure after the stream started | 200 + error block | see above |

## The agent → MCP side

Unchanged by the branch, and the same for both agents:

* MCP over streamable HTTP at `TICKETS_MCP_URL`, `Authorization: Bearer
  $MCP_API_KEY`; only `search_tickets` and `get_ticket` are exposed.
* Read-only calls carry **no user identity**: the MCP boundary authenticates the
  agent, not the person.
* With approvals enabled, `POST {mcp}/approvals/execute`
  `{"approval_token","request_id"}`; the token format is defined in
  [APPROVALS.md](APPROVALS.md#the-token) and verified by
  `mcp-server/src/approval.rs`.
