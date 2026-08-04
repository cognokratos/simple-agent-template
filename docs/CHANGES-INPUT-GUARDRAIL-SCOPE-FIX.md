# Input guardrail scope fix

This revision fixes false positives where the self-check LLM blocks legitimate
read-only alert queries such as:

- `Show me the complete details and all transactions for alert ALT-1001.`
- `Show all transactions for all open alerts.`

## Final policy precedence

1. Critical deterministic block rules always win.
2. Anchored read-only alert templates may override an LLM false positive.
3. All other requests use the LLM self-check verdict.

The allow templates match the complete user message, not a substring. A request
that appends prompt injection, secret extraction, evasion advice, or an unrelated
action will not qualify for the override.

## Trace fields

- `guardrail.llm.blocked`
- `guardrail.final.blocked`
- `guardrail.deterministic.blocked`
- `guardrail.deterministic.allow_matches`
- `guardrail.deterministic.allow_override_applied`
- `guardrail.decision_source`

An LLM false positive corrected by policy appears as:

```text
guardrail.llm.blocked = true
guardrail.final.blocked = false
guardrail.deterministic.allow_override_applied = true
guardrail.decision_source = deterministic_allow_override
```
