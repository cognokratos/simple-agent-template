# Input guardrail scope and false-positive fix

The self-check LLM sometimes classified legitimate read-only alert investigation
requests as unsafe because they contain words such as `transactions`, `AML`, and
`monitoring`. The final policy now uses explicit precedence:

1. Critical deterministic block patterns always block.
2. Narrow read-only alert requests are allowed, even if the LLM returns a false positive.
3. All remaining requests follow the self-check LLM verdict.

The allow override uses a small set of anchored, full-message templates for
read-only operations: listing open alerts, listing transactions for open alerts,
and retrieving a specific `ALT-*` alert. Because the complete message must match,
appending an instruction override or another unrelated action disables the override.

MLflow records both the raw classifier result and the final policy result:

- `guardrail.llm.blocked`
- `guardrail.final.blocked`
- `guardrail.deterministic.blocked`
- `guardrail.deterministic.allow_matches`
- `guardrail.deterministic.allow_override_applied`
- `guardrail.decision_source`

A corrected false positive appears as:

```text
guardrail.llm.blocked = true
guardrail.final.blocked = false
guardrail.deterministic.allow_override_applied = true
guardrail.decision_source = deterministic_allow_override
```

Disable the override with:

```env
GUARDRAILS_INPUT_READ_ONLY_ALLOW_OVERRIDE=false
```
