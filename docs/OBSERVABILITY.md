# Observability

One trace per request, covering the agent run and the safety decisions together.

## The problem this solves

An agent request is not one code path. It passes an input rail with its own
classifier call, one or more agent-model calls, a tool-policy decision per
proposed tool call, MCP calls, possibly a human-approval wait, and an output
rail. If those land in separate traces — or in none — the questions that
matter when an agent misbehaves cannot be answered: which layer refused, which
tool result the answer was built from, where the time went.

So every span of a request must descend from one root, and the decisions must
be on spans as structured attributes rather than in log lines.

The agent has one tracing model in one process: Rust `tracing` spans, exported
over OTLP/HTTP to the OpenTelemetry Collector by `tracing-opentelemetry`
([`telemetry/mod.rs`](../agent/src/telemetry/mod.rs)). Nothing has to be joined
after the fact. (The retired NAT agent, `nat-1.9-final`, had two exporters —
NAT's own and NeMo Guardrails' — and joined them by pre-seeding NAT's root span
through private framework attributes; see
[FRAMEWORK-MIGRATION.md](FRAMEWORK-MIGRATION.md#tracing).)

## How the trace is built

The workflow route ([`api/workflow.rs`](../agent/src/api/workflow.rs)) opens
the root span `support-tickets-agent.invoke` and spawns the run inside it. An
inbound W3C `traceparent` is honoured, so an instrumented caller's trace is
joined rather than replaced. Every span created while the root is current is
its child — the input rail and its classifier call, every `tool.policy`
decision, MCP calls, the approval gate, the output rail, and Rig's own `chat`
and `execute_tool` spans, because Rig adopts the current span as its agent
span. No private framework attribute is involved.

The trace id is also sent to the client: the `WORKFLOW_START` event's metadata
carries `workflow_trace_id`, `workflow_run_id`, `request_id` and
`agent_runtime: "rig-rust"`, which is how the evaluator links a result to its
trace.

Measured on the [request walkthrough](tutorials/REQUEST-WALKTHROUGH.md) prompt
(`qwen3:8b`, local Ollama):

```text
support-tickets-agent.invoke                    15.59 s  root: request.id, agent.runtime, input/output, final state
├─ guardrail.input.self_check                    2.31 s  decision, decision source
│  └─ guard_model.call → chat                    2.31 s  the classifier call (Rig)
├─ chat                                          2.71 s  Rig: the turn that proposed get_ticket
├─ execute_tool                                  0.05 s  Rig: running it
│  ├─ tool.policy                               <0.01 s  dispatch-hook verdict: allow
│  └─ tickets_mcp__get_ticket                    0.05 s  MCP call, redacted I/O
├─ chat                                         10.53 s  Rig: the turn that wrote the answer
└─ guardrail.output.stream                      13.26 s  overlaps the answer; counts only
```

The agent model's calls are separate `chat` spans, so model latency is read
directly off the trace: here 15.55 s of 15.59 s.

| Span | Created in | Attributes worth knowing |
| --- | --- | --- |
| `support-tickets-agent.invoke` | [`api/workflow.rs`](../agent/src/api/workflow.rs) | `request.id`, `request.id_source`, `execution.id`, `agent.runtime`, `enduser.roles`, `input.value`, `output.value`, `agent.execution.state`, `error.message` |
| `guardrail.input.self_check` | [`agent/input_rail.rs`](../agent/src/agent/input_rail.rs) | `guardrail.outcome`, `guardrail.blocked`, `guardrail.llm.blocked`, `guardrail.decision_source`, `guardrail.deterministic.matches`, `guardrail.deterministic.allow_matches`, `guardrail.input.sha256`, `guardrail.input.length` |
| `guard_model.call` | the same | `gen_ai.request.model` |
| `tool.policy` | [`agent/hooks.rs`](../agent/src/agent/hooks.rs) | `tool.name`, `policy.decision` (`allow`/`deny`), `policy.reason` |
| `tickets_mcp__<tool>` | [`mcp/tools.rs`](../agent/src/mcp/tools.rs) | `mcp.tool.name`, `tool.outcome`, `input.value`, `output.value` (redacted display copy) |
| `guardrail.output.stream` | [`agent/execution.rs`](../agent/src/agent/execution.rs) | `guardrail.outcome` (`passed`/`modified`/`blocked`), `guardrail.blocked`, `guardrail.modified`, `guardrail.masked_entities` (counts), `guardrail.pattern_index`, `guardrail.input.length`, `guardrail.output.length` |
| `approval.gate` | [`approval/gate.rs`](../agent/src/approval/gate.rs) | `approval.resource_id`, `approval.outcome`, `approval.override`, `approval.committed` |
| `human_approval.wait` | [`agent/scope.rs`](../agent/src/agent/scope.rs) | `interaction.id`, `interaction.kind`, `interaction.outcome` |
| `mcp.approvals.execute` | [`approval/gate.rs`](../agent/src/approval/gate.rs) | — |
| `human_approval.respond` | [`api/interactions.rs`](../agent/src/api/interactions.rs) | `interaction.outcome` |

With approvals enabled, `approval.gate`, `human_approval.wait` and
`mcp.approvals.execute` appear under `execute_tool`; the human's answer is its
own request, recorded as `human_approval.respond`. A request
refused by the input rail has no `chat`, `execute_tool` or output-rail span,
and its root records `agent.execution.state = blocked`.

## Readable content

The root span carries the readable question (`input.value`) and the readable
answer (`output.value`). The answer recorded is the **released** answer — the
text the output rail let through, masked and cut exactly as the client
received it — never the raw model text
([`execution.rs`](../agent/src/agent/execution.rs), `finish`). Recording the
model's text as "the answer" would let a masked or blocked response leak into
the trace through the root span. For a refused request, the answer recorded is
the refusal.

`agent.execution.state` is the final state of the run's explicit state machine
([`state.rs`](../agent/src/agent/state.rs)): `completed`, `blocked`, `failed`
or `cancelled`. It is recorded on every path, including a client disconnect,
which aborts the run and records `cancelled`. A failed run also records
`error.message` — the public failure class, not internals — whether or not
content capture is on: a failure signal is not request content, and a root span
with no output and no reason is what this pipeline exists to avoid.

Each text attribute is bounded by `AGENT_TRACE_CONTENT_MAX_CHARS`; a cut value
ends with `[trace content truncated]`.

**This is not proof of browser delivery.** "Recorded answer" means the text the
output rail handed to the response stream, not a confirmation that a byte
reached the client.

## Redaction, and what it does not cover

### What never reaches a span

* **Service credentials.** The gateway's `Authorization` header is compared in
  constant time and then removed from the request
  ([`api/auth.rs`](../agent/src/api/auth.rs)) before any handler runs, so no
  handler, log line or span can observe it.
* **The raw gateway identity.** `x-authenticated-user-id`,
  `x-authenticated-username` and `x-authenticated-email` are never recorded.
  The root records `enduser.roles` (a handful of shared values that explain an
  authorization outcome) and `request.id` (which joins a trace to its request),
  and — only when enabled — a [pseudonym](#per-user-attribution).
  `IDENTITY_HEADERS` and `RETAINED_GATEWAY_HEADERS` in
  [`identity.rs`](../agent/src/identity.rs) declare that decision per header,
  and `make static-check` fails if the gateway starts minting an
  `x-authenticated-*` header that is in neither list.
* **Approval tokens and secrets.** Never recorded; the integration test
  `no_credential_identity_or_approval_secret_reaches_a_span` runs a full
  approval and searches every exported span for them.
* **Secrets and PII in tool results.** The `tickets_mcp__<tool>` span records
  the same redacted display copy the UI's tool card receives
  ([GUARDRAILS.md](GUARDRAILS.md#tool-results-shown-to-people)); the model
  still reasons over the raw result.
* **Pre-rail model text, by default.** Rig can record prompts and completions
  on its own model spans (`gen_ai.prompt`/`gen_ai.completion`). That is off
  unless `AGENT_TRACE_CAPTURE_MODEL_CONTENT=true`, because the model's output
  there is *before* the output rail and can carry exactly the credential the
  rail exists to withhold.
* **Framework debug logging.** Rig logs the complete provider request — system
  prompt, conversation and every tool result — as a TRACE event. The export
  filter is fixed in code (`EXPORT_FILTER`: this service and Rig at `info`,
  everything else at `warn`) and deliberately not governed by `RUST_LOG`.
  Raising `AGENT_RUST_LOG` can put that text in the container log; it can never
  put it in the exported trace.

### What it does not cover

* **The user's own message is recorded as sent.** `input.value` on the root and
  `guardrail.user_input` on the input-rail span are not masked. A user who
  types a card number has put it in the trace store. Switch them off with
  `AGENT_TRACE_CAPTURE_CONTENT=false` and `GUARDRAILS_TRACE_CAPTURE_CONTENT=false`
  where that matters.
* **Redaction is the output policy's, with its limits.** Tool-result redaction
  uses the same secret patterns and PII recognisers as the output rail, so it
  misses what they miss — notably free-form PII
  ([GUARDRAILS.md](GUARDRAILS.md#pii--mask)).
* **Content capture is on by default.** The trace store holds the questions
  people ask and the answers they get. That is a data store with a retention
  and access problem, and this repository does not solve it for you.

## Per-user attribution

**Off unless `OTEL_TRACE_USER_ID=true`.** When on, the root span carries
`enduser.pseudonym`; it is the only per-user identifier a span can carry, and
the raw gateway headers are never recorded in either mode (above).

What the value is matters to that decision. It is not the gateway's identifier:
it is a UUIDv5 over `trusted-header:x-authenticated-user-id` and the user id,
in the agent's own namespace (`TrustedCaller::pseudonym` in
[`identity.rs`](../agent/src/identity.rs)). A stable pseudonym is a weaker
disclosure than a raw subject id, but it is not anonymity — it is the same
value for the same person on every request, so a trace store holding it can be
used to reconstruct one person's history of questions. The traces already
carry the question and the answer; the identifier is what turns them from a
corpus into a per-person record.

That is a decision for whoever operates the trace store and knows its access
controls and retention, which is why it is a switch rather than a default. Turn
it on where the backend is access-controlled and attribution helps triage.

When off, the attribute is absent rather than set to a placeholder: a literal
placeholder would become a "user" in backends that group traces by user.

## Configuration

| Variable | Default | Effect |
| --- | --- | --- |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | the collector | OTLP/HTTP endpoint (`OTEL_COLLECTOR_TRACES_ENDPOINT` is read if it is unset). With neither set, nothing is exported. |
| `OTEL_SERVICE_NAME` | `tickets-agent` | Service name on the exported resource |
| `DEPLOYMENT_ENVIRONMENT` | `local` | `deployment.environment` on the exported resource |
| `AGENT_TRACE_CAPTURE_CONTENT` | `true` | Readable question and answer on the root span, and tool input/output on tool spans. Disabling still records errors. |
| `AGENT_TRACE_CONTENT_MAX_CHARS` | `65536` | Per-attribute bound; truncation is marked |
| `AGENT_TRACE_CAPTURE_MODEL_CONTENT` | `false` | Rig's own prompt/completion content on model spans (pre-output-rail text) |
| `OTEL_TRACE_USER_ID` | `false` | Export `enduser.pseudonym`. See above. |
| `GUARDRAILS_TRACE_CAPTURE_CONTENT`, `GUARDRAILS_TRACE_MAX_CHARS` | `true`, `16384` | Content on the input-rail span ([GUARDRAILS.md](GUARDRAILS.md#configuration)) |
| `AGENT_RUST_LOG` | `tickets_agent=info,warn` | The container log only; never what is exported |

`AGENT_TRACE_CAPTURE_CONTENT` and `AGENT_TRACE_CONTENT_MAX_CHARS` were
`NAT_TRACE_CAPTURE_CONTENT` and `NAT_TRACE_CONTENT_MAX_CHARS`; the old names
are deprecated aliases ([CONFIGURATION.md](CONFIGURATION.md#renamed-variables)).
Booleans are parsed strictly: an unrecognised value stops startup.

The resource also carries `service.namespace = rig-rust-agent` and
`agent.runtime = rig-rust`.

## Verifying it

```
make verify-trace-pipeline   # offline: one trace per request, traceparent, redaction
make trace-test              # + live: real requests, asserted against MLflow
make traces                  # print the span tree of recent MLflow traces
```

`verify-trace-pipeline` runs the integration suite
[`agent/tests/observability.rs`](../agent/tests/observability.rs) on the host:
the production tracing layer against an in-memory exporter, because a no-op
tracer would return the ambient context unchanged and the parenting assertion
would pass without proving anything. It asserts that a request is one trace
with one root covering the input rail, the classifier call, Rig's spans, the
tool policy, the MCP call and the output rail; that an inbound `traceparent` is
joined; that no credential, raw identity or approval token reaches a span; that
the pseudonym is exported only when enabled; and that a blocked answer records
the released text, not the raw one.

`trace-test` ([`scripts/verify_traces_e2e.py`](../scripts/verify_traces_e2e.py))
needs the cluster and a model: it sends real requests and asserts the trace
shape in MLflow — one root, guardrail spans in the same trace, readable
question and answer, no credential material, bounded content.
