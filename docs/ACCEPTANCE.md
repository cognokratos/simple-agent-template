# Project status

What is actually built, what is actually verified, and what is not. Written for
someone deciding how much of this repository to trust.

The exhaustive control-to-command map is
[`VERIFICATION.md`](VERIFICATION.md). What the numbers *mean* is
[`EVALUATION_ANALYSIS.md`](EVALUATION_ANALYSIS.md). This page is the summary.

---

## Verified without a model

These need no GPU, no API key and no network. They should pass every time, and a
failure is a defect rather than variance.

| Area | Command | Result |
|---|---|---|
| Deterministic evaluation engine | `make rules-test` | 82 Rust tests, 20/20 labelled cases, 31/31 funds within range |
| Authentication gateway | `cargo test` in `gateway/` | 51 tests |
| Evaluator parsers and scorers | `make static-check` | 57 Python tests |
| Fixture, profile and policy integrity | `make etf-check` | 31 listings / 30 funds, unique IDs, valid ISINs, graded provenance, cross-listings consistent |
| Security-critical source wiring | `make static-check` | passes |
| SSE wire contract | `make verify-stream-adapter` | passes |
| Compose topology renders | `docker compose config` | passes |

`make static-check` runs the offline subset in one command, and
`.github/workflows/ci.yml` runs the whole table above on every push and pull
request without a single secret.

## Verified against a running cluster

These need Docker. The first three need no model.

| Area | Command |
|---|---|
| Human-approval boundary | `make verify-approvals` — 32 assertions against the real MCP mutation endpoint |
| Compose topology, isolation, auth, MCP key | `make security-test` |
| Observability pipeline | `make verify-trace-pipeline` |
| Guardrail rails, live | `make verify-guardrails` — needs a model |
| Human-initiated override, end to end | `make verify-hitl` — needs a model |
| Five live evaluation suites | `make eval-all` — needs a model, and **exits non-zero while the policy gate is red** |

## Not verified

Stated plainly rather than implied.

- **Live-suite figures are per-run, not guarantees.** The five model-dependent
  suites have been run against this implementation and their results are in
  `evaluation/results/<suite>-latest.json`, each naming the agent build, prompt
  version and model that produced it. Those numbers describe `qwen3:8b` on one
  run; they are not asserted here as invariants, and no figure for agent behaviour
  is repeated in prose where it could drift from the artifact.
- **Browser rendering.** No test opens a page.
- **Concurrency under load.** Row locking and one-time nonces are unit-tested;
  nothing races two approvals in anger.
- **Model generality.** The configuration is pinned to `qwen3:8b`; other models
  are documented as alternatives, not as verified.
- **Data currency, and data accuracy.** `make etf-check` verifies that every record
  cites a typed source with an ISIN locator and a retrieval date, and that no record
  claims stronger provenance than its URL carries. It cannot verify the record
  *against* that source: the figures are hand-transcribed, provenance is
  issuer-site level rather than document level, and there is no market-data feed by
  design. The issuer's current disclosure is the authority over any number here.
- **The entity model.** `etf_id` identifies an exchange listing. Rankings and
  summaries group by ISIN so one fund is one candidate, and the fixture validator
  asserts cross-listed rows agree on every scored field — but the storage model is
  still per listing rather than a fund with listings attached.

---

## The controls that matter

Each is enforced below the model and asserted automatically. Full detail in
[`VERIFICATION.md`](VERIFICATION.md).

| Control | Assertion |
|---|---|
| Policy lives in data, not code | reversing every band, component and threshold in `rules_spec.json` changes no score |
| An inconsistent policy cannot start | five distinct malformed specifications are each rejected at boot |
| The engine is authoritative by default | recomputed on every read and again at every write |
| Hard constraints survive a valid approval | a non-UCITS shortlist is refused with a genuine token and a rationale |
| Non-bypassable means non-bypassable | blocked for both decisions above reject, with and without the override flag |
| Incomplete data cannot shortlist | each of the five critical fields caps the decision individually |
| Quality without fit cannot shortlist | a fund earning under half the profile-fit weight is capped |
| A model may never promote | all nine ordered pairs; refused again inside the signed token |
| A human may override, with a rationale | both directions commit; an empty rationale is refused |
| A human can *initiate* an override | every decision is offered regardless of what the model proposed |
| Approvals are unforgeable, bound and single-use | forged, expired, misbound, stale and replayed tokens all refused |
| A refused change is reported as refused | the model is told in those words, never that it was applied |
| History is append-only | rejected by a database trigger, not only by convention |
| Untrusted text cannot reach policy | the decision is computed from typed columns no free text feeds into |

---

## The approval boundary is tested without an LLM

`agent/verify_approval_tokens.py` drives the real MCP mutation endpoint with
hand-minted tokens, using the **production** signer imported from
`nat_streaming_react.approval` rather than a copy — so a change to the token
format breaks the suite instead of silently diverging from it. It runs in about
two seconds and covers the whole rejection matrix plus five successful paths.

## The interactive path

`make verify-hitl` exercises the confirmation gate end to end with no browser: it
asks the agent to evaluate and decide on an ETF, answers the decision prompt by
choosing something *different* from what the engine arrived at, supplies the
mandatory rationale, and then chains `make verify-hitl-audit`, which asserts the
persisted history row field by field:

```text
actor_type=human | rules=research | llm≠shortlist | final=shortlist | override=1 | rationale=1
```

`llm_recommendation ≠ shortlist` is the assertion that matters. The promotion —
the direction the model may never take on its own — was initiated by a person.

The check deliberately does *not* demand `llm_recommendation = none`. That is a
stricter proxy than the property being described, and it contradicts the system
prompt, which tells the agent to send its recommendation on every commit. A model
that obeys records `llm_recommendation = research` — equal to the engine, so
still not a promotion — and would fail a check it should pass.

---

## Reproducing

```bash
make static-check   # offline: fixtures, evaluator tests, source wiring
make rules-test     # the deterministic engine (needs Rust)
make dev            # cold start of the full cluster
make test           # the cluster-plus-offline aggregate
make eval-all       # five live suites (needs a model)
make verify-hitl    # human-initiated override (needs a model)
```
