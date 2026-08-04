# Unified MLflow + OpenTelemetry observability

This version exports one canonical trace per user request.

## Trace architecture

```text
NAT canonical workflow root
├── NAT workflow / LangChain spans
├── MCP tool spans
└── NeMo Guardrails OpenTelemetry spans
        ↓
OpenTelemetry Collector
        ↓
MLflow /v1/traces
```

NAT remains the owner of the trace ID and root span. A pinned NAT 1.8 runner
patch pre-generates the root span ID and installs a matching non-recording
OpenTelemetry parent while the workflow runs. NeMo Guardrails uses the
process-wide SDK and therefore creates children of the NAT root instead of a
second trace.

## Why the runner patch exists

NAT 1.8 has two relevant behaviors:

1. Its native exporter reconstructs spans from NAT intermediate events and does
   not automatically expose that trace context to standard OpenTelemetry
   instrumentation.
2. Its streaming runner stores a preview list of up to 50 response chunks on the
   workflow end event.

`../agent/patch_nat_single_trace.py` backports a narrow fix for the pinned NAT 1.8
source:

- NAT and Guardrails share one trace ID and root span ID;
- the latest user message is stored as the root input;
- streamed `ChatResponseChunk.delta.content` values are concatenated into the
  root output;
- live streaming is unchanged because chunks are still yielded immediately;
- MCP and NAT child spans remain native and unchanged.

The patch fails the Docker image build if expected NAT 1.8 source fragments are
missing. Reassess or remove it when upgrading NAT.

## Services

- MLflow UI and OTLP receiver: `http://localhost:5000`
- OpenTelemetry HTTP receiver: `http://localhost:4318`
- Collector health endpoint: `http://localhost:13133`
- Agent API: `http://localhost:8000`
- assistant-ui: `http://localhost:3000`

MLflow persists its SQLite backend and artifacts in the `mlflow-data` volume.

## Start or upgrade

The agent image must be rebuilt because the NAT package is patched at build
time:

```bash
docker compose stop agent
docker compose build agent
docker compose up -d --force-recreate agent ui
docker compose logs -f agent otel-collector mlflow
```

Existing traces remain in MLflow and will still show the old two-trace shape.
Only new requests use the unified trace structure.

Open `http://localhost:5000`, select the **Default** experiment, and open
**Traces**.

## Expected result

For one prompt, MLflow should show one new trace row. The root input/output are
plain readable text. Opening **Details & Timeline** should show the MCP and
Guardrails children in the same trace.

The root span name is configured as:

```yaml
workflow:
  name: alerts-agent.invoke
```

## Content capture and privacy

Full question and answer capture is enabled for the local POC:

```env
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true
NAT_TRACE_CONTENT_MAX_CHARS=65536
```

The maximum applies only to the root trace preview; it does not truncate the
answer sent to the user.

Output Guardrails sanitize the final response but do not retroactively redact
MCP tool inputs and outputs already captured by NAT. Treat MLflow as sensitive
infrastructure and add exporter-side tool-span redaction before using real
customer data in a shared environment.

## Verification

Check the services:

```bash
curl -fsS http://localhost:5000/health
docker compose ps
docker compose logs otel-collector
```

Run the acceptance prompts in [`TEST-SCENARIOS.md`](TEST-SCENARIOS.md). Five
requests should create five traces, not ten.

Check that the old custom root span code is gone:

```bash
docker compose exec agent \
  python -c "import inspect, nat_streaming_react.register as r; print('start_agent_span' in inspect.getsource(r))"
```

Expected output:

```text
False
```

Check that the NAT runner patch loaded:

```bash
docker compose exec agent \
  python -c "import inspect; from nat.runtime.runner import Runner; print('NAT_MLFLOW_SINGLE_TRACE_PATCH' in inspect.getsource(inspect.getmodule(Runner)))"
```

Expected output:

```text
True
```

## Reset only MLflow data

```bash
docker compose down
docker volume rm "$(basename "$PWD")_mlflow-data"
docker compose up -d
```

Do not remove `postgres-data` unless the demo alert database should also be
reset.

## Guardrails verdict and self-check prompt capture

In addition to NeMo Guardrails' generic OpenTelemetry adapter spans, the local
middleware emits explicit summary spans that MLflow can render cleanly:

```text
guardrail.input.self_check
guardrail.output.regex_presidio
```

For the input self-check span, inspect:

- `guardrail.outcome`: `passed`, `modified`, or `blocked`;
- `guardrail.blocked` and `guardrail.modified`;
- `guardrail.prompt.rendered`: the rendered configured prompt;
- `guardrail.llm.prompt`: the prompt/messages captured from Guardrails' actual
  `llm_calls` generation log;
- `guardrail.llm.response`: the raw short model verdict when present;
- `guardrail.activated_rails` and `guardrail.log`.

The output span records `guardrail.regex.outcome` and
`guardrail.presidio.outcome`. Raw output before filtering is represented only by
its SHA-256 and length unless `GUARDRAILS_TRACE_CAPTURE_RAW_OUTPUT=true` is set.
