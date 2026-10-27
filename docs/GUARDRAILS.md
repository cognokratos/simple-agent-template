# Guardrails

The agent's policy layers are plain Rust in
[`agent/src/guardrails/`](../agent/src/guardrails), run by
[`input_rail.rs`](../agent/src/agent/input_rail.rs) and
[`execution.rs`](../agent/src/agent/execution.rs). Each is explicit about
whether its decision is probabilistic.

| Layer | Decides | Kind |
| --- | --- | --- |
| input ([`input.rs`](../agent/src/guardrails/input.rs)) | whether the request reaches the agent model | patterns + one **probabilistic** classifier call, combined deterministically |
| tool ([`tools.rs`](../agent/src/guardrails/tools.rs)) | whether a proposed tool call runs | deterministic ([SECURITY.md](SECURITY.md#tool-authority)) |
| output — secrets ([`output.rs`](../agent/src/guardrails/output.rs)) | whether the rest of the answer is released | deterministic |
| output — PII ([`pii.rs`](../agent/src/guardrails/pii.rs)) | which spans are masked | deterministic |

None of them decides whether a mutation is permitted: that authority is the MCP
server's, re-checked at the point of mutation. Guardrails filter text; they are
not authorization.

The retired NAT agent (`nat-1.9-final`) implemented the same input policy with
NeMo Guardrails and masked PII with Presidio; what changed in the migration,
and what was deliberately narrowed, is in
[FRAMEWORK-MIGRATION.md](FRAMEWORK-MIGRATION.md#input-guardrails).

## Input rail

An LLM self-check classifier, plus two deterministic layers that can override
it. Precedence is deliberately asymmetric:

1. a deterministic **critical-pattern** match always blocks;
2. otherwise a narrow, fully-anchored **read-only allow** template can correct an
   LLM false positive;
3. otherwise the LLM verdict stands.

The allow templates are anchored to the complete message (`^(?:…)$` on
whitespace-normalised text, at most 500 characters), so appending an
instruction override to an otherwise valid query does not inherit the allow.

The layers are pure functions — `critical_matches`, `allow_matches`, `screen`
and `resolve` — so the whole precedence table is a unit test that needs no
model. [`input_rail.rs`](../agent/src/agent/input_rail.rs) runs them in order
for each request:

1. the [length bound](#input-length-bound);
2. the deterministic layers;
3. one classifier call: the `self_check_input` prompt from
   [`agent/config.yml`](../agent/config.yml) (`guardrails.prompts`), rendered
   with the user message, sent as a single user message to the guard model
   (`LLM_GUARD_MODEL`) at temperature 0.001 with the prompt's `max_tokens`;
4. `resolve`, which applies the precedence above.

**The rail fails closed.** If the classifier call fails, the request is
refused and the decision event records
`classifier_available: false`; a missing verdict is treated like an
unrecognised one.

Which layer decided is recorded as `decision_source` on the
`guardrail_input_self_check_decision` stream event (which the evaluation
scorers read) and as `guardrail.decision_source` on the
`guardrail.input.self_check` span:

| `decision_source` | Meaning |
| --- | --- |
| `llm` | no deterministic layer matched; the classifier decided |
| `llm_and_deterministic_block` | a critical pattern matched and the classifier also voted to block |
| `deterministic_block_fallback` | a critical pattern matched; the classifier voted to allow |
| `deterministic_allow_override` | the classifier voted to block; an allow template corrected it |
| `llm_and_deterministic_allow` | the classifier allowed and an allow template agreed |
| `deterministic_input_limit` | refused by the length bound before anything else ran |

A refused request never reaches the agent model. The user sees the
classifier's own refusal (`I'm sorry, I can't respond to that.`) when the
classifier voted to block, and `GUARDRAILS_INPUT_BLOCK_MESSAGE` when only a
deterministic layer did or the classifier was unavailable.

### Client-supplied history

The gateway validates that each history message has role `user` or `assistant`;
it cannot validate who *wrote* it. A caller can therefore replay fabricated
assistant turns that no rail ever screened, carrying the implied authority of
the agent's own voice. The deterministic patterns run across those turns, and a
match joins the block set — labelled `history:<pattern>` — so it outranks the
allow override.

Prior **user** turns are deliberately not re-screened. Each was screened by the
full rail when it was the latest turn, and a refused one never reached the
model — but its text stays in the history the client replays. Screening it again
made one refusal poison the rest of the conversation: every later message,
however innocuous, matched the injection still sitting in the transcript.
Recovery after a blocked turn is a required behaviour.

The residual gap is a fabricated prior *user* turn, which no rail sees either.
It is knowingly left open: closing it means re-screening text the user can see
was already refused, and the same caller can simply send that text as the latest
turn, where the full rail does screen it.

## What the LLM verdict actually is

The self-check prompt demands `Yes` or `No`, and
[`classifier_says_safe`](../agent/src/guardrails/input.rs) turns the reply into
a decision. It reproduces NeMo Guardrails 0.21's `is_content_safe` parser,
which the retired NAT agent used, so the verdict means the same thing it did
before the migration. Its behaviour is pinned by the unit test
`the_verdict_parser_matches_nemo_0_21` rather than assumed, because the LLM half
of the input rail rests entirely on it:

* it lower-cases the reply, collapses every run of non-word characters to one
  space, keeps the **first two tokens**, and matches them against `safe`,
  `unsafe`, `yes`, `no` — token membership, not substring;
* **anything it does not recognise is unsafe.** An empty response, a refusal, a
  reply in another language and `Maybe` all block. This is the property that
  matters;
* the four keywords are tested in the order above, so a response whose first two
  tokens contain the bare word `safe` parses as safe even when negated —
  **`Not safe` parses as safe.**

That last one is pinned as a known hazard. Three things keep it unreachable
here: the prompt demands "exactly Yes or No and nothing else", `max_tokens: 4`
bounds the reply to roughly one word, and the deterministic critical-pattern
layer blocks the high-risk categories without consulting the model at all. If
any of those is relaxed, this parser stops being safe to rely on alone.

The classifier is the general instruct model with a self-check prompt, not a
dedicated guard model; replacing it is listed in
[LIMITATIONS.md](LIMITATIONS.md#before-production).

## Input length bound

The user text is measured before the guard model is asked anything. Over
`GUARDRAILS_INPUT_MAX_CHARS` (32,000 characters by default, floor 256) the
request is refused with `GUARDRAILS_INPUT_OVERSIZE_MESSAGE` and
`decision_source = deterministic_input_limit`.

Refused, never truncated. A classifier shown a prefix decides about the prefix,
so truncating would let 32k of benign text followed by the real request be
classified on the benign part alone. The bound also stops the cheapest possible
request from being the most expensive one to serve: the guard model runs on
every request, ahead of all other work.

`max_history: 20` in the workflow bounds the number of turns, not their length,
so it is not a substitute.

## Output rails

Two deterministic rails, applied **while the answer streams**, and they do
different things: secret patterns block, PII entities are masked. Only
assistant text from Rig's stream is a candidate for release; reasoning
fragments and tool-call arguments are never streamed to the client.

### A bounded streaming window

A secret can straddle any chunk boundary: checking each chunk alone would
release `sk-abc` in one chunk and `def…` in the next, each innocent.
[`StreamingOutputGuard`](../agent/src/guardrails/output.rs) therefore never
releases a character until 320 more have arrived behind it
(`HOLDBACK_CHARS`), and scans the held text together with the last 512
characters already released (`LOOKBEHIND_CHARS`):

```text
   released (raw kept as look-behind)    |  held back (not yet sent)
...............[    512 characters    ]  |  [ … | 320 characters ]
               \______ scanned on every fragment, secrets and PII ____/
```

Every pattern's minimal match is far shorter than the hold-back, so a match is
in view before any of it could be released; the release cut never splits a PII
entity. The limit, stated plainly: a match whose own span exceeds the window
(for example a keyword separated from its value by hundreds of spaces) can be
missed.

### Secrets — block

`guardrails.output.secret_patterns` in [`agent/config.yml`](../agent/config.yml):
credential shapes (`api_key=…`, `Bearer …`, `AKIA…`, `ghp_…`, `sk-…`,
private-key headers) and prompt-leakage phrases, matched case-insensitively. A
match stops the stream: what was held is dropped, and the client receives
"I can't share the rest of that response because it may contain sensitive
information." in its place. Nothing further is released, and dropping the
model stream cancels the rest of the run. Text released *before* the match
stays released; the hold-back is what makes sure none of the match was in it.

### PII — mask

`guardrails.output.pii_entities` lists the entities to mask; each match is
replaced with `<ENTITY_TYPE>` (`<EMAIL_ADDRESS>`, `<PHONE_NUMBER>`, …). The
detector is [`guardrails/pii.rs`](../agent/src/guardrails/pii.rs): pattern
matching tightened with checksums (Luhn for cards, mod-97 for IBANs,
Base58Check and Bech32 for Bitcoin addresses, area/group/serial rules for
SSNs). It is **not** an NER model. Checksums make it strict for entities that
have one; for free-form entities — phone numbers in layouts other than
international `+CC …` and North American `3-3-4` — it misses what a
context-aware detector would catch. This narrowing against the retired agent's
Presidio is recorded in [LIMITATIONS.md](LIMITATIONS.md) and
[FRAMEWORK-MIGRATION.md](FRAMEWORK-MIGRATION.md#output-guardrails-and-pii).

What the narrowing buys is a model-free output path: masking runs inside the
streaming window, so the answer is never buffered whole and still streams.

`PERSON` and `ORGANIZATION` are deliberately absent from the entity list: names
are part of the ticket-triage workflow, and masking them would destroy the
answer. They are also not supported by the detector, and an entity name it does
not recognise stops startup rather than being silently ignored.

### Oversized answers

`GUARDRAILS_PII_MAX_BUFFER_CHARS` (default 200,000) bounds one streamed answer.
Over it, the stream stops with "The response was too large to safely apply
output protection and the rest was withheld." What was already released stays
released; nothing past the bound is sent.

### Tool results shown to people

Output rails on assistant text alone would leave the UI's tool card and the
trace as a side channel. The agent therefore redacts secrets (`[REDACTED]`) and
masks PII in the *display copy* of every tool result
(`OutputPolicy::redact_for_display`) — the copy sent in `TOOL_END` events and
recorded on the `tickets_mcp__<tool>` span. The model still reasons over the
raw record.

## Configuration

Behaviour in [`agent/config.yml`](../agent/config.yml) (baked into the image;
`make rebuild-agent` after a change):

| Key | Effect |
| --- | --- |
| `guardrails.prompts` | must contain the `self_check_input` task; its `max_tokens` bounds the classifier reply |
| `guardrails.output.secret_patterns` | blocking patterns; must not be empty, and one that does not compile stops startup |
| `guardrails.output.pii_entities` | entities to mask; an unknown name stops startup; `[]` disables masking |

Switches and messages in the environment:

| Variable | Default | Effect |
| --- | --- | --- |
| `GUARDRAILS_INPUT_DETERMINISTIC_FALLBACK` | `true` | Deterministic critical-pattern blocking |
| `GUARDRAILS_INPUT_READ_ONLY_ALLOW_OVERRIDE` | `true` | Allow templates may correct an LLM false positive |
| `GUARDRAILS_INPUT_BLOCK_MESSAGE` | a refusal | What a user blocked by a deterministic layer sees |
| `GUARDRAILS_INPUT_MAX_CHARS` | `32000` | Ceiling on the user text handed to the guard model; a longer message is refused, never truncated. Floor of 256. |
| `GUARDRAILS_INPUT_OVERSIZE_MESSAGE` | a refusal | What a user over that bound sees |
| `GUARDRAILS_PII_MAX_BUFFER_CHARS` | `200000` | Ceiling on one streamed answer; over it the rest is withheld. Floor of 1024. |
| `GUARDRAILS_TRACE_CAPTURE_CONTENT` | `true` | The user message (`guardrail.user_input`) and the classifier reply (`guardrail.llm.response`) on the input-rail span |
| `GUARDRAILS_TRACE_MAX_CHARS` | `16384` | Bound on `guardrail.user_input` |
| `GUARDRAILS_TRACE_CAPTURE_RAW_OUTPUT` | `false` | See below |

Every boolean is parsed strictly: `true`/`false`, `1`/`0`, `yes`/`no`,
`on`/`off`. Anything else **stops startup**. A typo in
`GUARDRAILS_INPUT_DETERMINISTIC_FALLBACK` must not silently switch off the
deterministic block patterns, and keeping a default while a warning scrolls past
in a log is not much better.

`GUARDRAILS_TRACE_CAPTURE_RAW_OUTPUT` stays `false`. It is parsed (so a typo
still stops startup), but the output-rail span records counts and indices only,
never the matched or pre-mask text, so there is no raw output for it to enable.
Pre-rail model text can reach a span only through Rig's own model spans, which
`AGENT_TRACE_CAPTURE_MODEL_CONTENT` governs and which is also off by default
([OBSERVABILITY.md](OBSERVABILITY.md#what-never-reaches-a-span)).

## Verifying it

```
make verify-input-guardrails    # precedence, history forgery, verdict parsing, switches, config parsing
make verify-output-guardrails   # streamed release, cross-chunk secret blocking, PII masking
make verify-rails               # both rails end to end through the real service
make verify-guardrails          # all three
```

All of them are the agent's Rust tests, run on the host with no cluster and no
model. The unit tests cover every two-way split of a secret across chunks, a
secret streamed one character at a time, and the repository's own guardrail
fixtures (`db/guardrail_test_fixtures.sql`). `verify-rails` runs the
integration suite `agent_loop`, which drives the real service against a fake
OpenAI-compatible endpoint and an in-process MCP server, so what is under test
is the rail wiring rather than the model: a classifier block stops the request
before the agent model, an unreadable verdict fails closed, oversized input is
refused, a secret split across model chunks is never released, and tool
results shown to the client are redacted.
