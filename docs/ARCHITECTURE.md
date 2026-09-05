# Architecture and design decisions

Why the system is shaped this way, and which alternatives were rejected. The
controls themselves are in [`SECURITY.md`](SECURITY.md); the command that proves
each one is in [`VERIFICATION.md`](VERIFICATION.md); what to type to watch them
fire is in [`DEMO.md`](DEMO.md).

## Design objective

Keep investment policy deterministic while using the LLM for natural-language
intent, tool routing, explanation and comparison.

The distinction that shapes everything: an ETF score here is a **policy result**,
not a prediction. It expresses how well a fund matches a written mandate on a
dated snapshot of published facts. Nothing in the system forecasts anything, and
the parts most tempted to — the model, and the prose it produces — are the parts
held furthest from the decision.

## Trust boundaries

1. **Browser boundary** — the browser reaches Next.js and the Rust gateway. It
   cannot address NAT, MCP or PostgreSQL.
2. **Identity boundary** — the gateway authenticates against Keycloak and injects
   trusted user and request headers only after session validation.
3. **Model boundary** — the LLM is never an authorization source and never
   defines a decision.
4. **Policy boundary** — the Rust MCP independently recomputes the deterministic
   evaluation for every read and again immediately before every write.
5. **HITL boundary** — native NAT interaction pauses execution; a signed approval
   is required below the model layer.
6. **Persistence boundary** — state mutation, token consumption and the history
   event are one transaction.

## Policy is data, not code

`mcp-server/src/rules.rs` contains no ETF-specific thresholds, no weights and no
preference logic. It contains an interpreter for `data/rules_spec.json` and the
ordering `reject < research < shortlist`.

This is the difference between a rules engine and a pile of `match` statements,
and it buys three things:

- **A policy change is a reviewable diff in a JSON file.** Nobody rebuilds a
  container to change a cost band.
- **The policy can be validated.** The specification is parsed through
  `RulesSpec::parse` at boot, which refuses to start the service if component
  weights do not sum to 100, if a component's metric weights disagree with its
  declared weight, if the decision bands leave a gap or overlap, if a metric
  names an ETF field that does not exist, or if a numeric metric has anything
  other than exactly one fall-through band. A misconfigured policy fails at boot
  rather than silently rescoring the universe.
- **The same evaluation can be run against a different mandate.** Swap
  `investor_profile.json` and every score changes, with no code path aware that
  anything happened.

### Order must not be load-bearing

Numeric bands are selected by *bound*, not by position in the array. Each band
declares a `threshold`, exactly one declares `null` as the fall-through, and the
engine picks the band with the tightest satisfied bound.

This matters because the alternative — first match wins over the supplied
ordering — makes file layout into unreviewed policy. Appending a band above an
existing one silently changes decisions with nothing failing.
`rule_precedence_does_not_depend_on_file_order` reverses every band array, every
component and every decision threshold, and asserts that all 31 scores are
identical.

### Preferences that are off contribute nothing

An investor preference that is switched off does not penalise every fund; its
weight leaves the denominator entirely. Turning off "accumulating" must not make
the whole universe score worse — it must make the distinction stop mattering.
There is a test for exactly this, and it asserts the *direction*: with the
preference removed, a distributing fund scores strictly higher than it did.

## Missing data is a policy, not an accident

Real reference data has gaps, and the two obvious responses are both wrong.
Scoring an absent metric as zero says the fund is bad at something nobody
measured. Ignoring it silently publishes a score that is not comparable with the
others.

The engine does neither:

| Step | Behaviour |
|---|---|
| Absent metric | its weight is removed from the denominator, and the absence is reported with the weight it removed |
| Score | renormalised over the weight that was actually available |
| Completeness | published as a fraction of the declared scored fields |
| Critical field absent | the decision is capped at `research`, whatever the score |
| Completeness below 70% | the same cap |

The renormalisation has a known bias, and the cap exists because of it: removing
weight from the components a fund would have scored *badly* on inflates the
result. `AGGH-XETRA` is the shipped example — a global aggregate bond fund whose
issuer does not publish a top-ten concentration figure, which renormalises to 84
and would otherwise shortlist. The cap is not a safety net bolted on afterwards;
it is the counterpart to the renormalisation.

`tracking_difference_3y` is deliberately **not** a critical field. Every fund in
the shipped snapshot has it as `null`, so treating it as critical would cap the
entire universe at `research` and the cap would stop discriminating between
records. That reasoning is written into `rules_spec.json` next to the field list,
because a future contributor will otherwise "fix" it.

### Renormalisation is published, not implied

Because absent weight leaves the denominator, a component that *was* fully scored
contributes more than its nominal weight. Reporting that as a bare "16 points out
of a weight of 15" reads as arithmetic nobody checked, so each component publishes
four numbers instead of two:

| Field | Meaning |
|---|---|
| `nominal_weight` | what the specification assigns the component out of 100 |
| `available_weight` | how much of that this record could actually be scored on |
| `raw_earned_points` | earned out of `available_weight`, before rescaling |
| `normalized_contribution` | share of the published 0–100 score; these sum to it exactly |

`unavailable: true` marks a component with no data at all, which the contribution
column alone cannot distinguish from one that scored zero. The top-level
`normalization` block carries the same arithmetic once — total weight, available
weight, raw points, and the factor every point was multiplied by — so the score
can be reconciled by hand from what the response says.

## Naming that does not overclaim

Two component names were corrected because the obvious ones asserted more than the
data supports. This is not cosmetic: a score component's name is what a model
repeats to a user, and a model that says "strong liquidity" because AUM is large
has made a claim the system cannot support.

**`fund_scale`, not `scale_liquidity`.** AUM is a reasonable proxy for closure risk
and a rough one for secondary-market depth. It is not a measurement of bid/ask
spreads, average daily volume, order-book depth or primary-market creation
capacity — none of which this snapshot carries.

**`fund_structure` separate from `tracking_quality`.** Replication method and
realised tracking difference used to share one component called "tracking
quality", and since the realised figure is `null` throughout, the structure was
carrying the whole thing. So a physically replicated fund scored full marks for
tracking fidelity nobody had observed. They are now separate components:
`fund_structure` scores the methodology, `tracking_quality` scores only realised
tracking difference and therefore reports itself **unavailable** on every fund in
the shipped snapshot.

Splitting them changed no score — scoring is per metric and renormalisation is
over total available weight, so moving a metric between components is arithmetically
neutral. What changed is that the response no longer claims an observation it does
not have.

Replication is still read twice, and that is deliberate rather than double
counting: `fund_structure` asks "is this methodology sound?" and the
`physical_replication` investor preference asks "is it the one this investor asked
for?". Those are different questions, they are weighted separately, and
`preference_realisation` in `rules_spec.json` says so.

## Listing identity versus fund identity

`etf_id` identifies a **listing**: one share class on one exchange. `VUSA-LSE` and
`VUSA-XETRA` are the same Irish fund with the same ISIN, and the engine scores
them identically because every fact it reads is the same.

V1 stores listings, which is the right shape for the ambiguity this project tests
— a ticker is not unique, and a mutation must never land on a guessed listing. But
listing storage has a user-visible consequence that is not acceptable: as two
rows, one fund takes two slots in "the five highest-scoring ETFs" and hides a
genuine fifth candidate, and a summary counts it twice.

So the read models name both identities and the aggregations say which they mean:

- `identity.fund_identity` is the ISIN; `identity.listing` is the exchange and
  ticker.
- `search_etfs` returns one row per fund by default, naming the other venues in
  `other_listings_of_this_fund`, and takes `include_all_listings` for the listing
  view.
- `get_research_summary` publishes `universe.listings`, `universe.distinct_funds`,
  and both `by_deterministic_decision` and `by_deterministic_decision_per_fund`.
- The fixture validator asserts that cross-listed rows agree on every scored
  field, so one fund cannot produce two different evaluations.

Resolution is untouched. An ambiguous ticker is still reported as ambiguous rather
than resolved to whichever row the planner returned first, because collapsing a
*ranking* and guessing which listing a *mutation* meant are different problems
with different failure modes. A full entity/listing split — a fund table with
listings hanging off it — is the correct long-term model and is V2 work; nothing
in the current read models would have to change shape to get there.

## Quality and fit are different questions

The eight components split cleanly: six measure the fund, two measure the match
between the fund and the mandate. A high-quality fund that does not fit the
mandate is a real and common case, and averaging the two into one number hides
it — a euro corporate bond fund is genuinely excellent and genuinely wrong for a
twenty-year growth mandate.

`CAP-PROFILE-FIT` makes that explicit: earning less than half the available
profile-fit weight caps the decision at `research`. `IEAC-LSE` scores 76 and does
not shortlist, and the response says which cap fired and why.

The top-level weights are fixed at 20/20/15/9/6/10/10/10. Fit is only 20 of 100
because the quality components are also genuinely informative, and a cap is a
better tool than a weight for expressing "no amount of quality substitutes for
fit".

## Framework extension points, not framework patches

Every customization of NeMo Agent Toolkit and NeMo Guardrails is made through a
published extension point. No installed package is modified at build time, so
`pip install` output is reproducible and an upgrade is a dependency change rather
than a merge against vendored source.

| Need | Extension point used | Our code |
|---|---|---|
| Authenticate NAT's callers | `general.front_end.runner_class` — NAT imports this `FastApiFrontEndPluginWorkerBase` subclass and calls `build_app()` | `nat_streaming_react/fastapi_worker.py` |
| One trace per request | `ContextState.workflow_trace_id` + `ContextState._root_span_id` (the "eager trace linking" hook NAT's own eval runtime uses), plus W3C `traceparent` | `observability/trace_context.py` |
| Readable question/answer in traces | `nat.observability.processor.Processor`, inserted ahead of NAT's `Span → OtelSpan` conversion | `observability/trace_processor.py` |
| Ship spans to MLflow | `register_telemetry_exporter` plugin API | `observability/mlflow_exporter.py` |
| Guarded chat streaming | `register_middleware` + `GuardrailsMiddleware` subclass | `text_guardrails.py` |
| Blocking regex output rail | `LLMRails.register_action()` | `guardrails_compat.py` |
| Immediate final-answer streaming | `register_function` workflow component | `register.py` |
| Approval-gated mutations | `register_function` + NAT's native interaction manager | `approval.py` |
| Report what the agent is, for evaluation provenance | a route added to NAT's app inside the same `runner_class` worker | `provenance.py`, `fastapi_worker.py` |

The one remaining dependency on a non-public name is
`ContextState._root_span_id`. It is a public attribute of a public object,
documented in NAT's span exporter as an extension mechanism and used by NAT's own
evaluation runtime for the same purpose, but it is not covered by a stability
guarantee. `scripts/verify_security_sources.py` asserts its use so an upgrade
that removes it fails loudly instead of silently splitting traces.

### An upstream defect the extension work surfaced

Writing a *functional* regression test for the regex output rail — rather than a
configuration-level one — exposed a defect in the pinned `nemoguardrails` 0.21.
`_run_output_rails_in_streaming` resolves the `$bot_message` placeholder **in
place** in the shared flow configuration. The middleware holds one long-lived
`LLMRails`, so the first streamed response permanently rewrote
`text: "$bot_message"` to that response's literal text, and every later request
re-checked the *first* request's output.

The consequence is the bad kind: the secret-leakage output rail stopped
protecting every request after the first, for the lifetime of the container, with
nothing failing.

```text
1st benign:   released, not blocked          correct
2nd secret:   released, not blocked          LEAK
fresh rails:  blocked by regex check output  correct
```

`RailFlowParameterGuard` restores the pristine flow parameters before each rail
invocation. Upstream fixes this in 0.23.0 with a defensive copy.
`agent/verify_guardrails_rails.py` asserts **both** the fix and the underlying
defect, so the guard is provably load-bearing and the assertion fails once the
dependency can be upgraded — which is how a workaround should announce that it is
no longer needed.

The general point is that a configuration-level test would never have found this.
The rail was configured correctly the whole time.

## Observability

```text
NeMo Guardrails spans ─┐
                       ├─ same trace_id and root parent
NAT workflow/tool spans ┘
        │
        ├─ WorkflowContentProcessor           readable question/answer, bounded
        ├─ SensitiveHeaderRedactionProcessor  credential deny-list
        ├─ SpanToOtelProcessor + batching     NAT built-ins
        ▼
     OTLP/HTTP → OpenTelemetry Collector → MLflow
```

Two exporters feed the collector: NAT's own span exporter for the workflow tree,
and the process-wide OpenTelemetry SDK for Guardrails spans. They land in one
trace because the HTTP boundary fixes `(trace_id, root_span_id)` before NAT runs
and installs a matching `NonRecordingSpan` as the ambient OpenTelemetry parent.

A single distributed trace was chosen over merely correlated traces because it
was reachable through supported APIs. Correlation alone would let a reviewer find
the pieces of a request but not attribute latency across them — how long the
input rail delayed the first token is a parent/child question. Had a single trace
required patching NAT's runtime, correlation on `x-request-id` would have been
the better trade: brittle instrumentation is worse observability than a slightly
clumsier UI.

Streaming and observability are deliberately decoupled. The workflow function
yields each chunk to the client first and appends it to a bounded accumulator
afterwards, so nothing in the telemetry path can delay, reorder or buffer a
token. Capture happens in a `finally`, so a failure or a client disconnect still
produces a trace.

Observability and evaluation stay separate systems. MLflow is the trace backend
for the former and the dataset/judge/run store for the latter; the telemetry
changes above alter only what spans say, never what NAT streams on the wire,
which is what the evaluator consumes.

## Why MCP Resources and Tools are separate

Read-only policy and state context is exposed as Resources; executable and query
behaviours are Tools. This makes it easier for an MCP client or a reviewer to
reason about what is safe to read versus what can cause effects.

Both doors render through the same read models. When they did not, the resource
view omitted the deterministic evaluation and the provenance block that `get_etf`
was specifically built to carry — and the weaker path is exactly the one an
injected research note can talk over.

## What is stored, and what is recomputed

The deterministic result is **not stored anywhere**. It is recomputed from
`rules_spec.json`, `investor_profile.json` and the ETF row on every read and again
immediately before every write. Only the *committed* decision is persisted, and
only once a human approves one.

That split is easy to state and easy to lose. An earlier revision stored the
engine's score on `etfs.investment_score` at seed time so `search_etfs` could
`ORDER BY investment_score DESC` in SQL. Two things followed:

- A column documented as "null until a human approves a decision" was in fact
  populated for the entire universe from the moment the database came up.
- It went stale on the first edit to `rules_spec.json`, because nothing reseeds on
  a policy change — so the ranking reflected a policy generation nobody was
  running.

And because `etfs.decision` genuinely *was* null until approval, filtering
`search_etfs` by decision returned nothing at all on a fresh database, while the
tool description promised it would work.

The fix is not a better cache. SQL now answers only the filters that read stored
columns — query, provider, asset class, region, UCITS, distribution policy,
replication, review state, assignee, research-needed — and returns the candidates.
The server evaluates every candidate through the same `rules::evaluate`, filters on
the freshly computed decision and score, sorts by score descending with `etf_id`
ascending as the tie-break, collapses cross-listings, and applies the caller's
limit last. Ordering before filtering, or limiting before sorting, would each
silently drop the highest-scoring fund.

There is deliberately **no** SQL reimplementation of the scoring policy. With a
universe of a few dozen funds the cost of evaluating all of them is irrelevant next
to having one implementation of the policy in the repository.

The committed columns carry `decided_rules_version` and `decided_profile_version`,
so a historical decision names the policy that produced it. Without them a score of
84 from rules v1 and a score of 84 from rules v2 are the same integer, and any
report that mixes them is quietly incoherent.

## Audit events are one snapshot, or none

`audit_events` has a set of decision columns — `rules_decision`,
`llm_recommendation`, `final_decision`, `investment_score`, `rules_version`,
`profile_version` — and they are only meaningful as a *coherent* snapshot of one
evaluation.

The assignment path used to break that. It recomputed the current evaluation, wrote
the current `rules_version` and `profile_version`, and alongside them wrote the
committed decision and score from whenever the decision was actually taken. If the
policy had moved in between, the row read as a single snapshot and was two:

```text
investment_score: 84     <- earned under rules v1
rules_version:    2.0.0  <- in force at assignment time
```

Events that do not create a decision now leave those columns null and record what
they do know under `details.policy_generations`, as two separate objects each
naming its own versions. `ETF_ASSIGNED` is the case in the shipped system;
`ETF_EVALUATED`, `EVALUATION_COMMITTED` and `ETF_SHORTLISTED` all describe one
evaluation and populate the columns normally. `domain::policy_generations` is the
single helper, and its regression test moves the rules version between the decision
and the assignment and asserts the two generations stay apart.

## Context minimisation, and its floor

`search_etfs` returns compact metadata and a bounded result count rather than
full records. Detail, research context and history are fetched only when needed.

Minimisation has a floor: **a decision record must carry its own inputs.** A tool
that asks the model to explain a decision while withholding the premises does not
produce an absent explanation — it produces a confident and wrong one, assembled
from whatever related text is still in scope.

So `evaluate_etf` returns `etf_facts` alongside the result, and the general
policy strings live in a separate `policy` block labelled as applying to every
ETF. Nested inside a per-fund decision, a general constraint reads as a fact
about *that* fund; a model quoting "non-UCITS funds are rejected" from inside a
UCITS fund's evaluation has been set up to mislead.

## Who holds the default decision

Three roles, named separately everywhere they appear — approval token, MCP
validation, audit event, API response, approval prompt:

| Term | Source | Authority |
|---|---|---|
| `rules_decision` | the deterministic engine | **authoritative.** The default, always |
| `llm_recommendation` | the model | advisory only, in both directions |
| `requested_decision` | the authenticated human | final, with a rationale if it differs from `rules_decision` |

The commit path used to derive the default as
`llm_recommendation.unwrap_or(rules_decision)`. With the engine at `shortlist` and
the model at `research` that made `research` "the system decision", so a person
choosing `shortlist` — the engine's own answer — was recorded as a human override,
and a person choosing `research` was recorded as agreeing with the system. Exactly
backwards, and it made the model the decision authority in the conservative
direction while it was being refused authority in the optimistic one.

`rules::reconcile_decision` is now the only place the reconciliation happens. It is
pure, both mutation paths call it, and it returns a `DecisionAuthority` naming all
four values. `override_applied` is `requested_decision != rules_decision`, and the
token's own `override_requested` flag must agree, so an override cannot be smuggled
in either direction. The advisory ceiling — a recommendation may never outrank the
engine — is re-enforced in the same function, below the model.

`policy_comparison.default_decision` on `evaluate_etf` is the deterministic decision
in every case, including when the recommendation is *allowed*. Being permitted to
say something conservative is not the same as it taking effect, and the evaluation
suite has a metric — `rules_win_by_default_correct` — whose whole job is to catch
that distinction collapsing.

## Determinism versus model judgment

The model is useful for:

- mapping natural-language requests to MCP calls;
- explaining which components drove a score, from the returned numbers;
- comparing two funds along dimensions the tools actually returned;
- recommending a **more conservative** decision when grounded evidence warrants;
- narrating history.

The model is not trusted for:

- the decision;
- hard constraints;
- authorization;
- authenticated human identity;
- history persistence;
- any statement about future performance.

## HITL token protocol

The HMAC payload contains:

```text
version, expiry, action, etf_id,
actor_id, request_id,
rules_decision, llm_recommendation,
requested_decision, override_requested,
assignee, research_note, research_note_sha256,
override_rationale, nonce
```

### Nothing model-visible carries an approval reference

An earlier design had the mutation tools take the whole approved payload as
arguments, so the model had to replay it — including a long base64 token — across
a separate turn. It failed in a different way each time: the note was re-drafted,
a decision was dropped, and once the token came back the same length but not
identical, having been reproduced with a single character wrong.

Patching field by field was the wrong shape of fix. The API was reduced instead,
to `(etf_id, approval_token, request_id)`, and every other parameter is read from
the signed claims. The model has nothing left to restate, so it cannot drop,
reword or upgrade any part of what the human approved.

**Any long opaque string a model must copy verbatim is a failure mode**,
whichever field it happens to be. The current design goes further and gives the
model no approval reference at all: the approval-gated function applies its own
mutation, so nothing between the click and the state change depends on further
model output, and a candidate cannot end up approved but unchanged.

`research_note` travels inside the signed token and is persisted verbatim, so the
stored note is by construction the text the human approved rather than something
the model re-drafts on a later turn. `research_note_sha256` must match it, so
neither can be swapped for the other.

The MCP verifies exact equality against the attempted mutation. Changing the
note, the owner, the decision, the action, the ETF or the request invalidates the
token. The nonce is inserted into `consumed_approval_tokens` in the same
transaction as the mutation, so replay fails atomically.

### Why the deterministic result is bound into the token

The claims carry `rules_decision`, and verification refuses a token whose value no
longer matches what the engine returns, recomputed at the moment of the write.

The obvious reason is staleness: if the record changed, or the profile changed, or
the specification changed between the moment a person was shown a decision and the
moment they approved it, the approval is void. Consent is to a specific decision,
not to a button.

The less obvious reason is the one that matters more in practice. **`rules_decision`
is supplied by the model**, which read it from `evaluate_etf` — and nothing forces
the model to report it faithfully. A model that wants an outcome the advisory
ceiling forbids has an obvious move available: misreport the engine's decision so
that the outcome no longer looks like a promotion.

That is not hypothetical. Asked to shortlist the non-UCITS `VTI-ARCA`, `qwen3:8b`
first tried `llm_recommendation: shortlist` against an engine that returns `reject`
and was refused by the ceiling. It then retried with `rules_decision: "shortlist"` —
simply asserting a different deterministic decision — and that request *did* reach a
human, who approved it. The mutation was refused anyway:

```text
Human approval rejected: approval token was issued against a different
deterministic decision
```

The MCP never trusted the claim. It recomputed, got `reject`, and found the token
bound to `shortlist`. `VTI-ARCA` stayed `UNREVIEWED` with zero mutating history
events, and the agent was handed the explicit "REFUSED and NOTHING was applied"
instruction rather than being left to describe the outcome itself.

Two properties are worth separating here, because only the second one is
load-bearing. The advisory ceiling is a *policy* check on what the model may say.
The token binding is an *integrity* check on whether what the model said about the
engine is true. A system with only the first is defeated by a model that lies about
its premises; this one refuses before any state changes, and would refuse
identically if the model were fully captured by an injected instruction.

The hard constraint is a third, independent gate underneath both — `HC-UCITS` would
have refused this same mutation had the binding somehow passed. Which one fires
first depends on what the model got wrong.

## Failure behaviour

- Missing or invalid internal API key → `401` from the outermost ASGI layer,
  before NAT or MCP processing.
- Invalid rules specification → the MCP server refuses to start.
- Workflow failure → the root span records a readable error and the failing tool
  span keeps its error payload.
- Model recommendation more optimistic than the engine → refused, at the tool and
  again at the mutation.
- Model recommendation more conservative than the engine → allowed, recorded as
  advisory, and the engine's decision remains the default.
- Override flag disagreeing with whether the decision actually changed → refused,
  in either direction.
- Fixture citing a source whose `source_type` overstates its URL → the MCP server
  refuses to seed.
- Missing human approval → mutation rejected.
- Tampered, expired or replayed approval token → mutation rejected.
- Override without rationale → mutation rejected.
- Non-UCITS shortlist attempt with a valid token → mutation rejected.
- Shortlist without a research note → rejected.
- Assignment of a shortlisted fund without a research note → rejected.
- Assignment of an unreviewed or rejected fund → rejected.
- Database failure → the transaction does not commit.
