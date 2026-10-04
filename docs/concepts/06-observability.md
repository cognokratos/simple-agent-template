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
| Span source: agent | NAT intermediate steps → NAT spans → `agent_otlp` exporter ([`otlp_exporter.py`](../../agent/src/nat_streaming_react/observability/otlp_exporter.py)) |
| Span source: guardrails | NeMo Guardrails, through the process-wide OTel SDK |
| Collector | OpenTelemetry Collector ([`observability/otel-collector.yml`](../../observability/otel-collector.yml)) |
| Trace store and UI | MLflow |

## One request, one trace

NAT and NeMo Guardrails emit spans through two different exporters. Left alone,
they produce two unrelated traces per request.
[`trace_context.py`](../../agent/src/nat_streaming_react/observability/trace_context.py)
establishes one `(trace_id, root_span_id)` at the HTTP boundary, before either
sees the request, so both join the same tree. This took some engineering. The
reasons, and the private NAT attributes it relies on, are documented in
[OBSERVABILITY.md](../OBSERVABILITY.md#the-problem-this-solves).

The trace recorded for the [request walkthrough](../tutorials/REQUEST-WALKTHROUGH.md)
(*"Show me the complete details and history for ticket TKT-1001"*, default model,
local Ollama):

```text
support-tickets-agent.invoke                        17.6 s   workflow root (NAT)
  fastapi.dependencies / fastapi.endpoint
  <workflow>                                        17.6 s   the ReAct loop (NAT)
    guardrail_input_self_check_decision                      decision event
    tickets_mcp__get_ticket                         0.03 s   MCP tool call
    guardrail_output_regex_presidio_decision                 decision event
  guardrail.input.self_check     outcome=passed     2.79 s   input rail
    guardrails.request
      guardrails.rail
        guardrails.action
          self_check_input qwen3:8b                 2.78 s   guard-model call
  guardrail.output.regex_presidio  outcome=passed   0.05 s   output rails
    guardrails.request
      guardrails.rail → guardrails.action                    regex check
      guardrails.rail → guardrails.action           0.04 s   Presidio masking
```

The trace answers the latency question. The tool call took 30 ms and the rails
under 3 s combined, so most of the remaining ~14.7 s was the agent model. A warm
repeat on commit `af29ce0` had the same shape: 12.8 s total, 0.24 s input rail,
and still ~12.5 s of agent model. A
one-tool ReAct turn makes at least two model calls: one to choose the tool, one to
write the answer. Agent latency is almost
always model latency, and the fix is fewer model round trips, not faster SQL.

In this observed trace, the agent model's own calls did **not** appear as
separately named spans. Their time shows up only inside `<workflow>`. The
guard-model call did appear (`self_check_input qwen3:8b`). Check what *your*
trace contains rather than assuming.

## What gets recorded, and what does not

Traces carry the questions and answers people send. That makes the trace store a
data store with a retention and access problem. Decisions this repository makes:

| Data | Default | Why |
| --- | --- | --- |
| Readable question and *released* answer on the root span | on (`NAT_TRACE_CAPTURE_CONTENT`) | The answer is captured where the output rail releases it, so a masked or blocked answer never leaks into the root span |
| Credential headers (`authorization`, `cookie`, `x-api-key`, `x-csrf-token`, `x-authenticated-email`, ...) | redacted | `SensitiveHeaderRedactionProcessor` in [`trace_processor.py`](../../agent/src/nat_streaming_react/observability/trace_processor.py) |
| Per-user identifier | **off** (`OTEL_TRACE_USER_ID=false`) | A stable pseudonym turns a trace corpus into a per-person history |
| Pre-mask guardrail output | **off** (`GUARDRAILS_TRACE_CAPTURE_RAW_OUTPUT=false`) | It contains exactly what the rails exist to stop |
| Raw tool results in tool spans | **recorded** | Not covered by output rails. Use synthetic data, or add tool-span redaction before real PII reaches a shared backend. |

The last row is the honest gap. Header redaction is not content redaction. See
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
