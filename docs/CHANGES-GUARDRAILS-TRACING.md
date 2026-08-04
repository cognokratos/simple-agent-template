# Guardrails tracing changes

- Added `guardrail.input.self_check` spans with pass/block/modify status.
- Captures the rendered self-check prompt and Guardrails `llm_calls` prompt/completion.
- Added `guardrail.output.regex_presidio` summary spans.
- Records separate regex and Presidio outcomes as attributes and events.
- Keeps raw pre-mask output out of MLflow by default.
- Added environment flags and MLflow acceptance checks.
