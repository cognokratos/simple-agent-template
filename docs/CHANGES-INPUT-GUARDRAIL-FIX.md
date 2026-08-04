# Changes

- Fixed input Guardrails silently skipping `ChatRequest.messages`.
- Input self-check now evaluates the latest user message exactly once.
- Added exact self-check prompt and LLM call capture to MLflow.
- Added final/LLM/deterministic decisions and decision-source attributes.
- Added narrow high-confidence fallback patterns for critical inputs.
- Strengthened the binary self-check prompt with explicit examples.
- Bumped the local NAT plugin package to 0.1.7.
