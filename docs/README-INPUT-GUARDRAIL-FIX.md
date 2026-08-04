# Input Guardrail Fix

## Root cause

NAT 1.8's generic Guardrails target selector inspects only top-level string
fields on a Pydantic input. The chat workflow receives a `ChatRequest` whose
user text lives inside `messages: list[Message]`. As a result, the old
`pre_invoke` loop had zero items: the self-check LLM was never called, nothing
was blocked, and no input-guardrail span existed.

## Fix

The local `TextGuardrailsMiddleware.pre_invoke` now:

1. Extracts exactly the latest user message from `ChatRequest.messages`.
2. Always calls the configured `self check input` rail.
3. Captures the rendered prompt, actual logged LLM prompt/completion, activated
   rails, and parsed LLM decision.
4. Applies a narrow deterministic fallback for critical prompt injection,
   hidden-prompt/tool-secret extraction, and criminal financial evasion.
5. Emits one `guardrail.input.self_check` child span into the canonical NAT
   trace, including both the LLM and fallback decisions.

## Important trace fields

```text
guardrail.outcome
guardrail.blocked
guardrail.llm.blocked
guardrail.deterministic.blocked
guardrail.deterministic.matches
guardrail.decision_source
guardrail.prompt.rendered
guardrail.llm.prompt
guardrail.llm.response
guardrail.llm_call_count
```

## Rebuild

```bash
docker compose stop agent
docker compose build agent
docker compose up -d --force-recreate agent
docker compose logs -f agent otel-collector
```

Only the agent image needs rebuilding.

## Offline extraction/fallback smoke test

After rebuilding the agent:

```bash
docker compose exec agent python /app/verify_input_guardrails.py
```

Expected output:

```text
PASS block fixture: criminal_financial_evasion -> ['criminal_financial_evasion']
PASS block fixture: prompt_injection -> ['prompt_injection', 'system_prompt_or_tool_secret_extraction']
PASS allow fixture: no deterministic match
```

This test does not call the guard LLM. The end-to-end prompts in
`TEST-SCENARIOS.md` verify the rendered LLM prompt, completion, and final rail
outcome in MLflow.
