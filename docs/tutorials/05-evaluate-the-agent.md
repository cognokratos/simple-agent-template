# Lab 05 — Evaluate the agent

## Objective

Run the four evaluation suites, read a result as evidence (metric, latency
distribution, provenance), and write a case the agent currently fails.

## Concept

You cannot test a probabilistic component into correctness. You measure it
against a dataset, with deterministic scorers, and gate on the result. A red
metric is a finding, not a broken build.
→ [Concept 5](../concepts/05-evaluation.md)

## Architecture before

Manual prompts from labs 01–04. You saw failures, but nothing records them,
and nothing would notice if they got worse.

## Exercise

1. Read one dataset file per suite in
   [`evaluation/datasets/`](../../evaluation/datasets/). Each case has `inputs`
   (`question`, `case_id`) and `expectations` (whatever its suite's scorer
   reads).
2. Read the scorer for one suite in
   [`evaluation/scorers.py`](../../evaluation/scorers.py). `SCORERS` at the
   bottom maps suites to functions. Note that none of them calls a model, and
   none imports Phoenix or MLflow: each returns plain `Score` objects
   ([`evaluation/domain.py`](../../evaluation/domain.py)).

## Run it

```bash
make eval-list                 # suites and their gates
make eval SUITE=tools          # one suite
make eval-all                  # all four, gates on (exits non-zero if a gate fails)
make eval-all-allow-failures   # all four, metric gates off (unevaluated cases still fail)
make open-phoenix              # Datasets → tickets-agent-*-dataset@<digest> → Experiments
```

Each run writes `evaluation/results/<suite>-latest.json` (gitignored) — that
file is the result — and then publishes a copy to Phoenix. Nothing needs
creating in Phoenix first: the reporter mirrors the dataset revision on first
use. Stop Phoenix (`docker compose stop phoenix`) and run again: the result and
the exit status are identical, and `reporting.phoenix.status` says
`unavailable`.

## Observe

Open `evaluation/results/tools-latest.json`. The fields that matter:

| Field | Read it as |
| --- | --- |
| `required_metric`, `required_value`, `fail_threshold`, `passed` | The gate. `tool_call_correct/mean` must reach `1.0` by default. |
| `gate`, `errors` | The same decision with its `reason`, and any case that could not be evaluated (which fails the gate on its own). |
| `metrics` | Per-scorer means. `tool_name_match` vs `tool_argument_match` vs `tool_order_correct` tells you *how* a case failed. |
| `latency` | `min`, `p50`, `p95`, `max`. The mean is there too, but the tail is what users wait for. |
| `provenance.agent` | What actually answered: model, guard model, `build_commit`, prompt digests, `tools_exposed` |
| `provenance.harness` | The commit the harness ran from, and whether the tree was `dirty` |
| `provenance.consistent` | `false` means the running container is not this source tree |
| `cases[]` | Per case: the answer, the tool calls, every score with its *rationale*, the agent's trace id |
| `conditions.sha256` | Equal for two runs means they measured the same thing; `make eval-compare` diffs them |

In Phoenix, open the dataset, then the run's experiment. Each example shows the
observed output and the repository's scores as `CODE` evaluations with their
*rationale*: the expected and actual tool calls, the ungrounded numbers, the
matched patterns. The same scores are annotated on each case's agent trace
under **Traces**, so a failing case opens straight onto the request that
produced it. Phoenix shows the verdict; it did not compute it.

Results on the default model move between runs, which is why a single run is
an observation and not a verdict. One `make eval-all` run against this agent,
with `qwen3:8b` for both agent and guard, passed all four gates at 1.0. A
recorded run against the retired NAT agent (`nat-1.9-final`, built from commit
`af29ce0`, the same model, `provenance.consistent = true`) shows what red
gates look like and how to read them:

| Suite | Gate metric | Value | Gate | p50 | p95 | Failing case |
| --- | --- | --- | --- | --- | --- | --- |
| `guardrails` (9 cases) | `guardrail_correct/mean` | 1.000 | pass | 3.0 s | 12.6 s | — |
| `tools` (7) | `tool_call_correct/mean` | 0.857 | **fail** | 10.1 s | 12.8 s | `TOOLS-FANOUT-OPEN-TICKETS`: 3 of 5 `get_ticket` calls |
| `grounding` (4) | `grounded_in_tool_results/mean` | 0.750 | **fail** | 3.4 s | 12.5 s | `GROUND-ABSENT-RECORD`: answered "does not exist" with no tool call |
| `injection` (4) | `injection_resisted/mean` | 1.000 | pass | 4.3 s | 10.3 s | — |

Two red gates, each traceable to one case and one lab-04 experiment. That run
exited `0` because `ALLOW_FAILURES=1` suppresses only the metric gate. Plain
`make eval-all` would have exited non-zero. Run the suites a few times on your
own stack and compare: the same case can pass and fail on the same model.

## Break it

Write a case the agent can fail. Lab 04 showed `qwen3:8b` picking TKT-1004
(`high`) over TKT-1002 (`urgent`), and no suite covers prioritization. Add this
to [`evaluation/datasets/grounding.json`](../../evaluation/datasets/grounding.json):

```json
{
  "inputs": {
    "question": "Which ticket should we handle first, and why?",
    "case_id": "GROUND-PRIORITIZATION"
  },
  "expectations": {
    "required_tools": ["search_tickets"],
    "required_term_groups": [["TKT-1002"]],
    "forbidden_assertions": ["TKT-9999"]
  },
  "tags": { "category": "prioritization", "priority": "high" }
}
```

```bash
make eval SUITE=grounding ALLOW_FAILURES=1
```

The dataset's digest changes, so this result's `conditions.sha256` no longer
matches earlier grounding runs, and Phoenix receives the edited file as a new
dataset mirror.

Look at `grounding_required_facts_present` for your case versus
`grounded_in_tool_results`.

## Why it failed

When the model picks the wrong ticket, the case passes **grounding** (every
fact came from a tool) and fails **completeness** (the required term
`TKT-1002` is missing). If your run picked TKT-1002, run it again: the model
does not choose the same way every time. The suite gates on
grounding only, so this case alone does not turn the gate red. That is by
design ([EVALUATION.md](../EVALUATION.md#the-methodology-worth-reusing)):
inventing a fact and omitting one are different failures, and averaging them
hides which one moved.

So the dataset can *show* this failure, but it does not *gate* on it. If
"picks the right ticket" is a requirement, it needs its own metric and gate:
a decision-quality scorer, or a new suite. That is a design decision about what
your product promises, and it is one of the [challenges](../CHALLENGES.md).

Revert the dataset change unless you intend to keep it.

## Architecture after

```mermaid
flowchart LR
    DS[datasets/*.json] --> EV[evaluator] --> AG[live agent]
    EV --> SC{scorers} --> RES[(results/*.json)]
    RES --> GATE{gate}
    RES -->|copy| PH[(Phoenix)]
    RES -.->|copy, optional| ML[(MLflow)]
```

Every claim about agent quality now has a number, a dataset version and a
provenance record behind it. Full diagram: [concept 5](../concepts/05-evaluation.md#diagram-f-the-evaluation-pipeline).

## Comparing with the retired agent

Every result records `provenance.agent.runtime`: `rig-rust` for this agent.
Nothing in the harness, datasets, scorers or gates depends on the agent
framework, so the same suites can evaluate the retired NAT agent
(`nat-1.9-final`, recorded as `nat`) against the same model, and
`make eval-compare` (or Phoenix, filtering experiments by that metadata) can
compare the two ([EVALUATION.md](../EVALUATION.md#comparing-runtimes)).
That is the point of owning the evaluation in the repository: the framework
changed, the measuring instrument did not.

## What you learned

* Evaluation is a regression suite for behaviour, run against the real system.
* Deterministic scorers make a red metric a fact, but they only catch what they
  assert.
* Separate metrics for separate failures. Gate on the one you will act on.
* A result without provenance is not evidence.
* The platform displays the result; the repository decides it. `make eval-mlflow`
  (after `make dev-mlflow`) publishes the same result to MLflow without a second
  evaluation.

## Go deeper

* [EVALUATION.md](../EVALUATION.md), [evaluation/README.md](../../evaluation/README.md)
* [`.github/workflows/live-evaluation.yml`](../../.github/workflows/live-evaluation.yml)
  for running suites against a hosted endpoint
* Next: [Lab 06 — Debug with traces](06-debug-with-traces.md)
