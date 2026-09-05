# Verification map

Which control is enforced where, and which command proves it. The summary of what
is and is not verified is [`ACCEPTANCE.md`](ACCEPTANCE.md); the reasoning behind
the controls is [`ARCHITECTURE.md`](ARCHITECTURE.md) and
[`SECURITY.md`](SECURITY.md).

Every row names a command in this repository. Nothing here is asserted from a
transcript. Where a control cannot be checked automatically, it says so.

---

## Deterministic policy

| Control | Where it lives | Proven by |
|---|---|---|
| Policy is data, not code | `data/rules_spec.json`, interpreted by `mcp-server/src/rules.rs` | `rules-test` — `band_matching_does_not_depend_on_file_order` reverses every band, component and threshold and asserts all 31 scores are unchanged |
| An inconsistent policy cannot start the service | `RulesSpec::parse` → `validate` at boot in `main.rs` | `rules-test` — weights not summing to 100, a component disagreeing with its metrics, a gap in the decision bands, an unknown ETF field, and two fall-through bands are each rejected |
| Score components explain the score | `distribute` in `rules.rs` | `rules-test` — `reported_components_sum_to_the_published_score`, all 31 funds |
| Evaluation is pure | `rules::evaluate` | `rules-test` — byte-identical serialisation across repeated calls |
| Decision bands at their documented boundaries | `decision_thresholds` | `rules-test` — 0/49/50/74/75/100 |
| Text matching is case-insensitive both ways | `rules.rs`, `seed.rs` | `rules-test` — a shouted record scores identically to a canonical one, and every seeded value is asserted canonical |
| `etf_id` is the only unique identifier | `store::resolve_etf_ids` | `rules-test` asserts the snapshot keeps a cross-listed ticker; `etf-check` asserts unique IDs and a duplicated ticker |

## Hard constraints

| Control | Proven by |
|---|---|
| A non-UCITS fund is rejected however well it scores | `rules-test` — `VTI-ARCA` scores 88 and rejects |
| Every non-UCITS fund in the snapshot rejects | `rules-test` — 6/6 |
| A constraint only applies when the profile enables it | `rules-test` — disabling `require_ucits` restores the score decision |
| A non-bypassable constraint blocks every decision above reject, for every actor | `rules-test` — both decisions × both values of the override flag |
| A record that cannot answer a constraint does not satisfy it | `rules-test` — a boolean requirement against a text field fails closed |
| The constraint is re-enforced at the moment of the write, on every mutation path | `verify-approvals`, plus `verify-security-sources` asserting `blocking_hard_constraint` is called in each mutation body |
| A valid human approval cannot lift it | `verify-approvals` — shortlist *and* research on a non-UCITS fund, both with genuine tokens and rationales |

## Missing data

| Control | Proven by |
|---|---|
| Absent metrics leave the denominator rather than scoring zero | `rules-test` — removing a metric moves the score by ≤3 points, not by its full weight |
| The absence is reported, with the weight it removed | `rules-test` |
| A missing critical field caps the decision at research | `rules-test` — each of the five critical fields, individually |
| A cap can only ever make a decision less attractive | `rules-test` — swept across all 31 funds |
| An unrecognised value is reported, not silently scored | `rules-test` |
| The shipped snapshot contains a real capped shortlist | `rules-test` — `AGGH-XETRA`, raw 84 → research |
| Quality without fit cannot shortlist | `rules-test` — `IEAC-LSE`, raw 76 → research |

## Decision authority

| Control | Proven by |
|---|---|
| A model may be equal or more conservative, never more optimistic | `rules-test` — all nine ordered pairs |
| **The deterministic decision is the default, whatever the model recommended** | `rules-test` — `reconcile_decision` derives it from the engine alone; `verify-approvals` case A commits engine `shortlist` / model `research` / human `shortlist` with `override_applied = false` |
| A permitted conservative recommendation does not become the default | `rules-test` — `default_decision` is the deterministic decision for all nine pairs; the evaluator's own test fails a comparator that adopts the recommendation |
| Confirming the engine's decision is never recorded as an override | `verify-approvals` case A′ — the same claims asserting an override are refused |
| Following a conservative model *away* from the engine is an override | `verify-approvals` case B — commits with `override_applied = true` and a rationale |
| The policy is re-enforced below the model, inside the signed token | `verify-approvals` — a promotion carried in valid claims is refused |
| A human may override in either direction, with a rationale | `verify-approvals` — a downgrade and a promotion both commit and record the rationale |
| An override without a rationale is refused | `verify-approvals` |
| The override flag must agree with the decision it claims | `verify-approvals` — both directions |
| A human can *initiate* an override, not merely ratify one | `verify-hitl` — every decision is offered regardless of what the model proposed; needs a model |
| A human promotion is not refused for an argument the model omitted | `verify-approvals` — the prompt plan asks the person for the grounded note when no note was drafted, so shortlist is reachable without the model having been there first |
| The model did not propose the promotion | `verify-hitl-audit` — asserts `llm_recommendation ≠ shortlist` while `override_applied = 1` |
| A non-bypassable constraint holds against a fully valid human override | `rules-test` case E, and `verify-approvals` × 3 |

## Current result versus committed record

| Control | Proven by |
|---|---|
| The deterministic result is never stored, so it cannot go stale | `rules-test` — the seeder writes no decision and no score; a fresh database has 31 nulls |
| Search ranks and filters on the current deterministic result | `rules-test` — every decision and every score filter returns results on an unreviewed universe |
| A committed value cannot move the ranking or satisfy a score filter | `rules-test` — a planted committed score of 100 changes neither |
| A rules or profile change is reflected immediately, with no reseed | `rules-test` — ranking and filtering both follow the altered policy |
| Ranking order is total and reproducible | `rules-test` — score descending, `etf_id` ascending; the limit is applied last |
| A committed decision names the policy that produced it | `verify-approvals` — `decided_rules_version` / `decided_profile_version` written with the score |
| A non-decision event never merges two policy generations | `rules-test` — the rules version changes between commit and assignment; `verify-approvals` asserts the assignment result keeps them apart |

## Listing identity

| Control | Proven by |
|---|---|
| One economic fund occupies one slot in a ranking | `rules-test` — a top-five contains five distinct ISINs |
| The other listings of a fund are named, not dropped | `rules-test` — `other_listings_of_this_fund` |
| The listing view remains available | `rules-test` — `include_all_listings` returns more rows than the grouped default |
| An ambiguous ticker is still reported rather than guessed | `rules-test`, and the MCP resolver's ambiguity path |
| Cross-listed rows cannot disagree on a scored field | `etf-check` — asserted across all fourteen economic fields |

## Data provenance

| Control | Proven by |
|---|---|
| Every record cites a typed source with a locator and a retrieval date | `etf-check`, `rules-test` |
| A source cannot claim stronger provenance than its URL carries | `rules-test` — a bare host claiming `issuer_factsheet` is refused; enforced again at MCP boot by `validate_sources` |
| A record with no usable provenance cannot be seeded | `rules-test` — the MCP server refuses to start |

## Approval tokens

| Control | Proven by |
|---|---|
| Forged signature refused | `verify-approvals`, and `cargo test` in `approval.rs` |
| Expired token refused | both |
| A lifetime beyond the server's own ceiling refused | `cargo test` — the verifier does not trust the minter's TTL claim |
| Bound to the exact action and ETF | both |
| Bound to the authenticated request | both |
| Bound to the deterministic decision the human was shown | both — a stale decision voids the token |
| The research note matches its own hash | both |
| Consumed exactly once, atomically with the mutation | `verify-approvals` — a replayed nonce returns 409 |
| Missing audit identity refused | `cargo test` |
| A refused change is reported to the model *as refused* | `verify-approvals` |

## State machine

| Transition | Proven by |
|---|---|
| `UNREVIEWED` + reject / research / shortlist | `verify-approvals` |
| `RESEARCH` → `SHORTLISTED` via approved promotion | `verify-approvals` |
| `RESEARCH` / `SHORTLISTED` → `ASSIGNED`, decision unchanged | `verify-approvals` |
| `ASSIGNED` → `ASSIGNED` on reassignment | `verify-approvals` |
| `REJECTED` cannot be assigned | `verify-approvals` |
| `UNREVIEWED` cannot be assigned | `verify-approvals` |
| A decided candidate cannot be re-decided | `verify-approvals` |
| An already-shortlisted candidate cannot be shortlisted again | `verify-approvals` |
| Shortlisting requires a research note | `verify-approvals` |

## Trust boundaries

| Control | Proven by |
|---|---|
| NAT, MCP, PostgreSQL and the gateway publish no host ports | `network-test` |
| assistant-ui can reach only the gateway | `network-test` |
| NAT cannot reach PostgreSQL directly | `network-test` |
| Exact network membership matches the reviewed topology | `security-config-test` |
| Unauthenticated gateway and NAT requests are refused | `auth-test` |
| MCP refuses a missing key and accepts the agent's | `verify-mcp` |
| The credential is stripped before any handler or exporter sees it | `verify-security-sources`, plus the constant-time comparison test |
| Login redirects carry PKCE, `state` and the exact callback | `auth-test` |
| CSRF is enforced on state-changing browser requests | `cargo test` in `gateway/` |
| Logout cannot be undone by a refresh completing afterwards | `cargo test` — generation-checked session write-backs |
| The browser cannot invent a decision | `cargo test` — only the three decisions and an explicit cancel are forwarded |

## Untrusted text

| Control | Proven by |
|---|---|
| Free text is structurally separated and carries a provenance label | `rules-test` / read-model shape; `eval-injection` measures the behaviour |
| The deterministic decision is unreachable from injected text | `eval-injection` — the decision holds across five attack shapes |
| No mutation is attempted under injection | `eval-injection` |
| No credential or prompt heading is disclosed | `eval-injection` |
| A poisoned record is still answerable | `eval-injection` — over-blocking is scored as a failure |
| HTML in assistant prose is escaped, not executed | `rehype-raw` is deliberately absent; verified by hand, not by CI |

## Guardrails

| Control | Proven by |
|---|---|
| Hostile prompts are blocked deterministically as well as by the model | `verify-input-guardrails` |
| Ordinary investment questions are not blocked | `verify-input-guardrails` — fees, comparisons, override requests, assignment |
| An attack appended to a valid query does not benefit from the allow override | `verify-input-guardrails` |
| A refused turn does not poison the rest of the conversation | `verify-input-guardrails` |
| A forged assistant turn in client-supplied history is screened | `verify-input-guardrails` |
| Structured ETF evidence survives the output rail | `verify-output-guardrails` |
| Credentials and private keys are blocked | `verify-output-guardrails`, `verify-rails` |
| The output rail is not defeated by the upstream flow-parameter defect | `verify-rails` — asserts both the fix and the underlying defect |
| The rail runs per request, not on a shared instance | `verify-output-guardrails`, `verify-rails` |

## History and provenance

| Control | Proven by |
|---|---|
| `audit_events` rejects `UPDATE` and `DELETE` | the database trigger in `db/init.sql` |
| Every event records the rules and profile versions in force | schema + `verify-approvals` |
| A read-only evaluation cannot write a correlation ID | by construction — `evaluate_etf` records none |
| A result names the agent that produced it | `eval-all` — a stale container reports `consistent: false` |
| The baseline artifact self-certifies its inputs | `rules-test` — fixture SHA-256, rules version, profile version |

## Observability

| Control | Proven by |
|---|---|
| Guardrails and NAT spans land in one trace | `verify-trace-pipeline`; `trace-test` end to end |
| Credential headers are redacted before export | `verify-trace-pipeline`, `verify-security-sources` |
| Streaming is never delayed by telemetry capture | `verify-security-sources` — asserts capture happens *after* the chunk is yielded |
| Numeric answer chunks survive the SSE wire | `verify-stream-adapter`, and the evaluator-side test |

---

## Not covered automatically

Stated plainly rather than implied.

- **Browser rendering.** No test opens a page. `verify-hitl` covers the
  interaction protocol underneath the UI; `verify-stream-adapter` covers the wire
  contract.
- **Concurrent mutation under load.** Row locking and one-time nonces are
  unit-tested; no test races two approvals in anger.
- **Model generality.** Every model-dependent number is `qwen3:8b`.
- **Live data accuracy.** `etf-check` validates that each record cites a source
  and a snapshot date. It cannot check the record against that source; there is
  no feed.
- **Prose quality.** Grounding and completeness are deterministic; fluency is not
  scored, and the reasoning is in `EVALUATION_ANALYSIS.md`.

---

## Reproducing everything

```bash
make static-check   # no Docker, no cluster, no model
make rules-test     # the deterministic engine (needs Rust)
make dev            # cold start
make test           # fixtures, engine, evaluator, guardrails, traces, security, approvals
make eval-all       # five live suites with strict gates (needs a model)
make verify-hitl    # human-initiated override, end to end (needs a model)
```
