# 6. Observability and traces

This page covers learning-path stage 7.

## Why logs are not enough for agents

In a conventional service, the code path for a request is fixed. You read logs
to see which branch it took. In an agent, **the model chooses the code path at
runtime**: which tools, in which order, how many times. Logs scattered across
four services cannot answer the questions you actually ask when an agent
misbehaves:

* What did the model see when it chose that tool?
* Which tool call returned the data the wrong answer was built from?
* Did the input rail decide, or the model? Which layer of the rail?
* Where did the 17 seconds go?

A **trace** answers these questions by recording the whole request as one tree
of timed spans, so the shape of the agent's decision appears directly.

| Concept | Implementation in this repo |
| --- | --- |
| Tracing standard | OpenTelemetry (OTLP/HTTP) |
| Span source | Rust `tracing` spans in the agent, Rig's own `chat`/`execute_tool` spans among them, exported by `tracing-opentelemetry` ([`telemetry/mod.rs`](../../agent/src/telemetry/mod.rs)) |
| Collector | OpenTelemetry Collector ([`observability/otel-collector.yml`](../../observability/otel-collector.yml)) |
| Trace store and UI | MLflow |

## One request, one trace

The workflow route ([`api/workflow.rs`](../../agent/src/api/workflow.rs)) opens
the root span `support-tickets-agent.invoke` and runs the whole request inside
it. Every span created while it is current becomes its child: the input rail,
the classifier call, Rig's model calls and tool executions (Rig adopts the
current span), each `tool.policy` decision, the MCP call and the output rail.
One tracing model in one process means one tree, with nothing to join after
the fact. An inbound W3C `traceparent` is honoured, so an instrumented caller's
trace is joined. Why this matters, and what each span carries:
[OBSERVABILITY.md](../OBSERVABILITY.md#the-problem-this-solves).

The trace recorded for the [request walkthrough](../tutorials/REQUEST-WALKTHROUGH.md)
(*"Show me the complete details and history for ticket TKT-1001"*, default model,
local Ollama):

```text
support-tickets-agent.invoke                        15.59 s   workflow root
  guardrail.input.self_check   llm_and_deterministic_allow   2.31 s   input rail
    guard_model.call → chat  qwen3:8b                2.31 s   guard-model call
  chat  qwen3:8b             (chooses get_ticket)    2.71 s   agent model, turn 1
  execute_tool                                       0.05 s   Rig runs the tool
    tool.policy              allow                  <0.01 s   deterministic verdict
    tickets_mcp__get_ticket  ok                      0.05 s   MCP tool call
  chat  qwen3:8b             (writes the answer)    10.53 s   agent model, turn 2
  guardrail.output.stream    passed                 13.26 s   output rail (overlaps the answer)
```

The trace answers the latency question. The tool call took 50 ms and the
policy decision under 10 ms; the two agent-model calls and the classifier call
account for 15.55 s of 15.59 s. A one-tool turn makes at least two agent-model
calls: one to choose the tool, one to write the answer. In this run, agent
latency was model latency, so the fix is fewer model round trips, not faster
SQL. The output-rail span is long because it lives as long as the answer
streams, not because the rail is slow.

Check what *your* trace contains rather than assuming: a refused request has
no `chat` span at all, and an approval adds `approval.gate` and
`human_approval.wait` under `execute_tool`.

## What gets recorded, and what does not

Traces carry the questions and answers people send. That makes the trace store a
data store with a retention and access problem. Decisions this repository makes:

| Data | Default | Why |
| --- | --- | --- |
| Readable question and *released* answer on the root span | on (`AGENT_TRACE_CAPTURE_CONTENT`) | The answer recorded is what the output rail released, so a masked or blocked answer never leaks into the root span |
| Service credential (`Authorization`) | never recorded | Removed from the request once verified, before any handler runs ([`api/auth.rs`](../../agent/src/api/auth.rs)) |
| Raw gateway identity (`x-authenticated-user-id`, `-username`, `-email`) | never recorded, whatever `OTEL_TRACE_USER_ID` says | Attribution is the pseudonym below, never the raw subject. Roles and the request id are kept. |
| Per-user identifier | **off** (`OTEL_TRACE_USER_ID=false`) | A stable pseudonym turns a trace corpus into a per-person history |
| Tool results in tool spans | **redacted display copy** | The same copy the UI's tool card shows: secrets redacted, PII masked by the deterministic recognisers |
| Model prompts and completions on Rig's spans | **off** (`AGENT_TRACE_CAPTURE_MODEL_CONTENT=false`) | Model output there is pre-output-rail: it can carry exactly what the rails exist to stop |
| Rig's full provider request (logged at TRACE) | never exported | The export filter is fixed in code; `AGENT_RUST_LOG` affects only the container log |

The honest gaps: redaction is only as good as the output policy's patterns and
recognisers, and the user's own question is recorded as typed, unmasked. See
[OBSERVABILITY.md — redaction](../OBSERVABILITY.md#redaction-and-what-it-does-not-cover).

## Traces and evaluations reinforce each other

* An evaluation tells you **that** a case failed. The trace for that run tells you
  **why**: which tool, which arguments, which rail decision.
* The guardrail spans carry structured attributes (`guardrail.outcome`,
  `guardrail.decision_source`, `guardrail.deterministic.matches`) that you can
  assert on. [TEST-SCENARIOS.md](../TEST-SCENARIOS.md#guardrails-observability-acceptance-checks)
  lists them per scenario.
* `make trace-test` ([`scripts/verify_traces_e2e.py`](../../scripts/verify_traces_e2e.py))
  sends real requests and asserts on the resulting trace shape: one tree,
  guardrail spans present, readable I/O, no credentials.

## Go deeper

* Lab: [06 — Debug with traces](../tutorials/06-debug-with-traces.md)
* Reference: [OBSERVABILITY.md](../OBSERVABILITY.md),
  [TEST-SCENARIOS.md — trace-shape acceptance](../TEST-SCENARIOS.md#trace-shape-acceptance-test)
* Next concept: [7. Security and trust boundaries](07-security-and-trust-boundaries.md)
