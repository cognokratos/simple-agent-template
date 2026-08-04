# Live MLflow Evaluation

This project contains two independent MLflow GenAI evaluation suites. Every
dataset row calls the running NeMo Agent Toolkit endpoint, so the evaluation
covers the real Guardrails, ReAct, MCP, PostgreSQL, and streaming behavior.

## Evaluation architecture

```text
MLflow evaluation dataset
        ↓ one live request per row
Evaluator predict_fn
        ↓ HTTP/SSE
NeMo Agent Toolkit agent
        ├── input Guardrails
        ├── ReAct / Ollama
        ├── MCP tools
        └── output Guardrails
        ↓ structured live result
Deterministic MLflow scorers
        ↓
Dedicated MLflow evaluation experiment
```

The evaluator does not call the MCP server or database directly. It only calls
the public NAT workflow endpoint and observes the same behavior as assistant-ui.

## Suites

| Suite | MLflow experiment | MLflow dataset | Primary gate |
|---|---|---|---|
| Guardrails | `alerts-agent-guardrails-evaluation` | `alerts-agent-guardrails-dataset` | `guardrail_correct/mean = 1.0` |
| Tool calling | `alerts-agent-tool-calling-evaluation` | `alerts-agent-tool-calling-dataset` | `tool_call_correct/mean = 1.0` |

The names can be changed through `../.env`.

## Source-controlled datasets

```text
evaluation/datasets/guardrails.json
evaluation/datasets/tool_calling.json
```

The Guardrails dataset includes malicious prompts and legitimate requests. This
measures both malicious-prompt recall and benign-request false positives.

The tool dataset defines exact expected tool names and expected argument
subsets. The multi-alert scenario requires `search_alerts` first, then accepts
the individual `get_alert` calls in either order.

## Machine-readable live metadata

NAT already exposes tool calls as intermediate `TOOL_*` or `FUNCTION_*` events.
The custom Guardrails middleware additionally emits compact events named:

```text
guardrail_input_self_check_decision
guardrail_output_regex_presidio_decision
```

These events contain the final decision only. They do not duplicate the full
self-check prompt or raw pre-mask output. The detailed prompt, LLM completion,
and Guardrails diagnostics remain in the MLflow trace spans.

assistant-ui ignores these function names, so they do not appear as user-facing
tool calls.

## Makefile commands

The shortest workflow is:

```bash
make dev
make eval-bootstrap-replace
make eval-all
```

Individual suites:

```bash
make eval-guardrails
make eval-tools
```

Exploratory run without failing the shell command:

```bash
make eval-all-allow-failures
```

Custom options remain available through the generic target:

```bash
make eval SUITE=guardrails RUN_NAME=guardrails-qwen3-8b-v1 FAIL_THRESHOLD=0.95
```

Run `make help` for Docker lifecycle, logs, health checks, fixtures, smoke tests,
and every evaluation target.

## Start the application

```bash
make up
```

The equivalent raw command is `docker compose up -d`.

Wait for the agent, MLflow, Collector, MCP server, and PostgreSQL to be ready.
MLflow is available at:

```text
http://localhost:5000
```

## Create or synchronize datasets

The `run` command automatically synchronizes its dataset. You can also do it
explicitly:

```bash
docker compose --profile evaluation run --rm evaluator \
  python -m evaluation bootstrap --suite all
```

Replace all existing records with the JSON source files:

```bash
docker compose --profile evaluation run --rm evaluator \
  python -m evaluation bootstrap --suite all --replace
```

`merge_records()` matches records by their complete `inputs` object, so changing
a question or case ID creates a new dataset record. Use `--replace` after
removing or renaming test cases.

## Run evaluations

Guardrails only:

```bash
docker compose --profile evaluation run --rm evaluator \
  python -m evaluation run --suite guardrails
```

Tool calling only:

```bash
docker compose --profile evaluation run --rm evaluator \
  python -m evaluation run --suite tools
```

Both suites, each in its own experiment and run:

```bash
docker compose --profile evaluation run --rm evaluator \
  python -m evaluation run --suite all
```

Custom run name:

```bash
docker compose --profile evaluation run --rm evaluator \
  python -m evaluation run \
  --suite guardrails \
  --run-name guardrails-qwen3-8b-v1
```

The command exits nonzero when the suite's primary metric is below the default
threshold of `1.0`. This makes it suitable for CI. During exploratory work:

```bash
docker compose --profile evaluation run --rm evaluator \
  python -m evaluation run --suite all --allow-failures
```

## Guardrails scores

Each row produces:

- `guardrail_correct`
- `guardrail_decision_event_present`
- `guardrail_false_positive`
- `guardrail_false_negative`
- `no_tools_when_expected_blocked`

The primary metric is the mean of `guardrail_correct`. A perfect regression run
has:

```text
guardrail_correct/mean = 1.0
guardrail_false_positive/mean = 0.0
guardrail_false_negative/mean = 0.0
```

## Tool-calling scores

Each row produces:

- `tool_call_correct`
- `tool_name_match`
- `tool_argument_match`
- `tool_order_correct`
- `unexpected_tools_absent`
- `duplicate_tool_calls_absent`
- `tool_call_count_match`

Expected arguments are matched as subsets by default. For example, an actual
call containing `{"status": "open", "limit": 50}` satisfies an expectation of
`{"status": "open"}`.

## Live prediction output

The prediction function returns a structured object to MLflow:

```json
{
  "answer": "I found two open alerts...",
  "blocked": false,
  "guardrail": {
    "input": {
      "outcome": "passed",
      "blocked": false,
      "decision_source": "llm_and_deterministic_allow"
    },
    "output": {
      "outcome": "passed",
      "blocked": false,
      "modified": false
    }
  },
  "tool_calls": [
    {
      "name": "search_alerts",
      "arguments": {"status": "open"}
    }
  ],
  "agent_trace_id": "..."
}
```

The evaluation trace therefore has readable inputs and outputs, while
`agent_trace_id` links the result to NAT's full observability trace containing
LLM, MCP, and Guardrails spans.

## Concurrency and timeouts

Local Ollama evaluations run sequentially by default:

```env
MLFLOW_GENAI_EVAL_MAX_WORKERS=1
```

Increase this only when the model server and hardware can support concurrent
requests. Other controls:

```env
EVALUATION_HTTP_TIMEOUT_SECONDS=360
EVALUATION_HTTP_MAX_ATTEMPTS=4
```

The prediction function is explicitly decorated with `@mlflow.trace`, so the
project sets:

```env
MLFLOW_GENAI_EVAL_SKIP_TRACE_VALIDATION=true
```

This avoids MLflow making an extra validation prediction before the dataset run.

## Add a test case

Add a JSON object to the relevant dataset file. Every record needs `inputs` and
normally includes `expectations`:

```json
{
  "inputs": {
    "question": "Show alert ALT-1001.",
    "case_id": "TOOLS-NEW-CASE"
  },
  "expectations": {
    "expected_tool_calls": [
      {"name": "get_alert", "arguments": {"alert_id": "ALT-1001"}}
    ],
    "order_mode": "exact",
    "arguments_match": "subset",
    "allow_unexpected_tools": false
  },
  "tags": {
    "category": "single_tool_details"
  }
}
```

Then synchronize with `bootstrap --replace` or run the suite normally to merge
it.
