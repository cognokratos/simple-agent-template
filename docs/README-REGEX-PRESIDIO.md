# Regex + Presidio output guardrails

This version removes the LLM-based `self check output` rail while preserving the existing LLM-based input self-check.

Output processing is now deterministic:

- `regex check output` blocks common API keys, access tokens, private-key headers, explicit system/developer prompt leakage, and instruction-override text.
- `mask sensitive data on output` uses Microsoft Presidio to redact email addresses, phone numbers, payment-card numbers, IBANs, IP addresses, crypto addresses, and US SSNs.
- `PERSON` and `ORGANIZATION` are intentionally not masked because customer and analyst names are legitimate data in this alert-investigation POC.

The output rails make no LLM calls. `stream_first: false` checks each buffered streaming segment before releasing it. The buffer is 80 tokens with 24 tokens of context, which is large enough for most identifiers while retaining progressive output.

## Rebuild

Presidio adds Python packages and a spaCy model, so rebuild the agent image:

```bash
docker compose stop agent
docker compose build agent
docker compose up -d --force-recreate agent
docker compose logs -f agent
```

A clean rebuild is useful if the dependency layer is unexpectedly reused:

```bash
docker compose build --no-cache agent
```

## Basic tests

Normal alert output should preserve business and analyst names:

```text
Show me all transactions for all open alerts.
```

A response containing an email address should display a masked value. A response containing a configured secret pattern should be replaced by the standard guardrail refusal.

Adjust `score_threshold` in `../agent/config.yml` if Presidio produces false positives. Raising it reduces masking; lowering it catches more possible identifiers.

## Guardrails 0.21 streaming compatibility

NeMo Guardrails 0.21 has two upstream regex-output streaming defects: the action does not accept dispatcher-injected keyword arguments, and its structured result is not mapped to a blocked verdict. The agent image applies NVIDIA's minimal fixes from Guardrails PRs #1932 and #1937 during the build. This avoids changing NAT 1.8's compatible Guardrails version.
