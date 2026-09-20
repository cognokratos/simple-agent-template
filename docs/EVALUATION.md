# Evaluation

Four MLflow suites driven against the **running** agent, with deterministic
scorers. No LLM judges: a red metric is a fact about the run rather than an
opinion about it.

## Suites

| Suite | Question | Gate |
| --- | --- | --- |
| `guardrails` | Did the input policy block what it should and allow what it should? | `guardrail_correct/mean` |
| `tools` | Did the agent call the right tools with the right arguments? | `tool_call_correct/mean` |
| `grounding` | Is the answer built only from what the tools returned? | `grounded_in_tool_results/mean` |
| `injection` | Does adversarial text in a *tool result* change the answer, the tools called, or the state? | `injection_resisted/mean` |

Datasets are source-controlled JSON under `evaluation/datasets/` and synchronised
into MLflow, so a case is reviewable in a pull request.

## The methodology worth reusing

**Grounding and completeness are reported separately, and only grounding is
gated.** Asserting a value no tool returned is a failure of integrity; omitting a
figure the question asked for is a failure of thoroughness. Averaging them hides
which one moved and pins the gate to a value the system does not reliably hold,
so it goes permanently red and stops signalling anything.

**Numbers are compared by value, not by digit run.** A tool returns `980.0` and
the answer writes `$980.00`; a digit-run comparison calls that ungrounded. That
false positive is what makes such a metric useless, so the scorer normalises and
falls back to a digit-substring check for identifiers embedded in larger tokens.

**Claim detection suppresses negations within the clause.** The answers this
system *wants* are full of "nothing was changed" and "no ticket was resolved". A
scorer that flags the disclaimer alongside the claim fails every well-behaved
answer. Position matters, not mere presence: "the ticket was escalated, though I
am not certain" is still a claim.

**A state report is not the same claim as a performed action, and the scorer
checks each differently.** "The ticket was resolved" and "TKT-1005 is high
priority" describe a *condition*, which `action_claims` verifies against the
`status`/`priority` fields the matching `get_ticket`/`search_tickets` call
actually returned — true when it matches, a violation when it does not or
when there is no evidence for that ticket at all. "I changed its priority" and
"the ticket has been escalated" claim an *event* occurred; there is no ticket
field whose value means "changed", so these stay unconditional violations
regardless of whether the resulting value happens to match reality — a
coincidentally correct end state does not make "I did this" true. Only the
structured `ticket`/`tickets` fields on a tool result count as evidence:
free text (`description`, a history event's `summary`) is never read, so a
fabricated approval planted in stored text cannot become authoritative by
being echoed back. Ticket attribution (which claim belongs to which id) is
resolved per claim, using whichever id most recently appeared at or before
that claim's own position — an id named later in the same sentence never
becomes the retroactive subject of an earlier claim, and an explicit shared
subject ("TKT-1001 and TKT-1002 are urgent priority") checks every id named.
A claim is only exempted as someone else's quotation ("the note claims
'...'") when it sits inside an actual quotation mark *and* the clause uses
reporting language — attribution language alone, with nothing quoted
("according to the ticket record, X"), is still checked against evidence,
not excused. Both are still heuristics, not parsing — see the comments above
`_state_claims` in `scorers.py` for the specific cases this does and does
not handle.

**Guardrail false positives and false negatives are counted separately.** They
are different failures with different costs and must not average.

**Over-blocking is a failure in the injection suite.** Refusing to read a record
because its stored text is hostile denies the support agent a real record. The
defence is that the untrusted text cannot reach the tools called, the state, or
any credential — not that the request is refused.

**Injection fixtures are seeded, not poisoned.** Dedicated resolved `TKT-INJ-*`
rows, referenced by nothing else. The obvious implementation mutates a demo
record before the suite and restores it afterwards — and then a suite that
crashes leaves the demo corrupted. With dedicated rows there is nothing to
restore, because nothing is ever mutated, and every suite stays read-only.

## Provenance

A result recording its dataset, metrics and latency but not the agent is not
evidence of anything reproducible. Three identities are collected, from the three
places that each know one:

| Identity | Source | Why there |
| --- | --- | --- |
| `agent` | the running container's authenticated `GET /version` | the only source that describes what actually answered |
| `prompts` | MLflow's prompt registry, from the mounted `agent/config.yml` | a registered version can be diffed; MLflow stores the link itself |
| `harness` | the Makefile, on the host | the evaluator container has no `.git` and no working tree |

The interesting field is `consistent`. The evaluator digests the prompt *file*
while the agent reports a digest of the prompt it *loaded*; a disagreement means
the container is not running this tree — the exact drift a host-side `git
rev-parse` conceals.

`dirty` is `bool | None`, and `None` means "not observable" rather than "clean".
Equating those would let a tree nobody inspected claim a verified checkout.
`GIT_DIRTY` deliberately excludes `evaluation/results`, because those files are
the *output* of a run: counting them would make every run after the first report
a dirty tree on account of the previous run's artifacts.

`/version` reports digests and model *names*, never prompt text and never a
credential. `scripts/verify_security_sources.py` asserts that.

## Latency

Reported as a distribution — min, p50, p95, max — not a mean. MLflow aggregates
feedback to a mean, and a mean is the least useful latency statistic because it
hides the tail, which is what a user waits for. Nearest-rank percentiles: on
suites of four to ten cases an interpolated percentile invents values that were
never measured.

## Output

Each run writes `evaluation/results/<suite>-latest.json` with metrics,
threshold, pass/fail and the full provenance record, so the artifact is
self-describing on its own. **The directory is gitignored**: nothing generated is
committed, so a CI artifact can never be confused with a stale checked-in result.

## Running it

```
make eval-list                 # suites, experiments, datasets
make eval-bootstrap            # create or merge the MLflow datasets
make eval SUITE=grounding      # one suite
make eval-all                  # everything, with gates
make eval-test-host            # the harness's own unit tests, no Docker
```

`ALLOW_FAILURES=1` suppresses the **metric** gate only. An unreachable agent, a
missing dataset or a dead model still raises and fails. That separation is the
point: a red metric is a published finding, a broken cluster is a broken build,
and the two must not report identically.

A read-only evaluation that reaches a human-approval wait raises immediately
rather than blocking until the socket times out, so a dataset defect does not
present as an infrastructure failure.

## Adding a case

Append to the suite's JSON. `inputs.question` and `inputs.case_id` are required;
`expectations` is whatever that suite's scorer reads. Then
`make eval-bootstrap SUITE=<suite>`.

Adding a **suite** is a dataset, an entry in `evaluation/config.py`, and an entry
in `SCORERS` in `evaluation/scorers.py`.

## Generalizing for a domain application

Nothing in the core is domain-specific. Override:

| Variable | What it binds |
| --- | --- |
| `EVALUATION_TOOL_NAMES` | tool names the harness recognises as tool calls |
| `EVALUATION_MUTATING_TOOLS` | tools that change state (empty here: the sample is read-only) |
| `EVALUATION_MODEL_PREFIX` | how the deployed agent is grouped in MLflow |
| `*_EVALUATION_EXPERIMENT` / `*_EVALUATION_DATASET` | per-suite MLflow names |
| `EVALUATION_SYSTEM_PROMPT_NAME` / `EVALUATION_RAIL_PROMPT_NAME` | prompt-registry names |

Domain vocabulary belongs in the dataset — `required_term_groups`,
`forbidden_assertions`, `forbidden_strings`, `required_tools`,
`forbidden_tools` — never in the scorer.
