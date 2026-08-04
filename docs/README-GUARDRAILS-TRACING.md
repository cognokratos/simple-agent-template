# Guardrails decision and prompt tracing

This project augments NeMo Guardrails' generic OpenTelemetry adapter with two
explicit summary spans emitted by the local `text_guardrails` middleware.
Both spans inherit NAT's canonical workflow trace context, so they appear in the
same MLflow trace as the workflow and MCP tools.

## Input self-check span

Span name:

```text
guardrail.input.self_check
```

Important attributes:

```text
guardrail.outcome              passed | modified | blocked
guardrail.blocked              true | false
guardrail.modified             true | false
guardrail.prompt.rendered       rendered self_check_input prompt
guardrail.llm.prompt            prompt/messages from GenerationLog.llm_calls
guardrail.llm.response          raw guard-model completion when available
guardrail.activated_rails       detailed activated rail log
guardrail.log                   normalized Guardrails generation log
```

The span's `input.value` contains the user message and rendered guard prompt.
Its `output.value` contains the raw short guard-model verdict, activated rails,
and parsed decision.

The middleware requests both `activated_rails` and `llm_calls` through
`GenerationLogOptions`, then records the response before returning or blocking.

## Output deterministic span

Span name:

```text
guardrail.output.regex_presidio
```

Important attributes:

```text
guardrail.outcome              passed | modified | blocked
guardrail.regex.outcome        passed | blocked
guardrail.presidio.outcome     passed | modified | skipped
guardrail.blocked              true | false
guardrail.modified             true | false
```

Two `guardrail.rail_result` events record the individual regex and Presidio
outcomes. The output value contains only the sanitized result by default.

## Content and privacy controls

```env
GUARDRAILS_TRACE_CAPTURE_CONTENT=true
GUARDRAILS_TRACE_CAPTURE_RAW_OUTPUT=false
GUARDRAILS_TRACE_MAX_CHARS=16384
```

`GUARDRAILS_TRACE_CAPTURE_CONTENT=true` is required to display the rendered
self-check prompt and guard-model response. These values may contain user data.

`GUARDRAILS_TRACE_CAPTURE_RAW_OUTPUT` remains false because pre-mask output can
contain PII or secrets. Enabling it should be limited to synthetic local tests.

## MLflow inspection

Open:

```text
Default experiment → Traces → one request → Details & Timeline
```

Select `guardrail.input.self_check` to inspect the exact prompt and verdict.
Select `guardrail.output.regex_presidio` to inspect the deterministic output
rail results. The Events tab contains the concise per-rail decision events.
