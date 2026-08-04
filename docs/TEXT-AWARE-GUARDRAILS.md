# Text-aware Regex + Presidio streaming fix

This version fixes two NeMo Guardrails 0.21 / NAT 1.8 streaming compatibility issues:

1. `mask_sensitive_data` did not accept the extra runtime keyword arguments passed by the streaming action dispatcher.
2. NAT's generic Guardrails middleware converted each `ChatResponseChunk` with `str(chunk)`, causing regex and Presidio to inspect Pydantic object representations rather than assistant text.

## Changes

- Adds local middleware type `_type: text_guardrails`.
- Extracts only `choices[*].delta.content` from NAT chat chunks.
- Sends clean text through NeMo Guardrails streaming output rails.
- Re-wraps sanitized text as `ChatResponseChunk` with the original response ID/model metadata.
- Backports the Presidio `**kwargs` compatibility fix.
- Retains the existing regex streaming fixes.
- Extracts the latest user message cleanly instead of stringifying the entire `ChatRequest`.

The output configuration remains conservative:

```yaml
streaming:
  enabled: true
  chunk_size: 80
  context_size: 24
  stream_first: false
```

This means output is streamed in sanitized blocks. Sensitive values are checked and masked before a block is sent to the browser.

## Rebuild

```bash
docker compose stop agent
docker compose build agent
docker compose up -d --force-recreate agent
docker compose logs -f agent
```

No UI, MCP server, or database rebuild is needed.

## Expected logs

You should still see:

```text
Executing registered action: detect_regex_pattern
Executing registered action: mask_sensitive_data
```

You should no longer see:

```text
mask_sensitive_data() got an unexpected keyword argument 'context'
ChatResponseChunkChoice(...)
```

inside Presidio action errors.
