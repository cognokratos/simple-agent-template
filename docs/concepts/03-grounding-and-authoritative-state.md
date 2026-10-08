# 3. Grounding in authoritative systems

This page covers learning-path stage 4.

## The problem: the model's memory is not your database

A model asked *"what is the priority of TKT-1004?"* without tools can only do one
of two things: say it doesn't know, or produce a plausible guess. A guess is
indistinguishable from a fact in fluent prose. In a support-ticket domain, a
plausible guess about refund status is worse than no answer.

**Grounding** means every factual claim about domain state is derived from data
the agent retrieved from the system of record during this request, and the
answer can be checked against that data.

| | Model memory | Authoritative system |
| --- | --- | --- |
| Source | Training data, the prompt | PostgreSQL, through MCP tools |
| Freshness | Frozen at training time | Current at the time of the call |
| Knows *your* tickets | No | Yes |
| Can be checked | No | Yes. The tool result is in the trace. |
| Who decides it is true | Nobody | The database (constraints, transactions) |

## How this repository grounds answers

1. **The prompt demands it.** The `system_prompt` in
   [`agent/config.yml`](../../agent/config.yml) begins: *"You must use the
   available tools for every factual statement about tickets or their
   history,"* and asks the model to *"distinguish recorded facts (what a tool
   returned) from your own recommendations."*
2. **The tools are the only path to the data.** The agent holds no database
   credentials. Facts can enter the conversation only through `search_tickets`
   and `get_ticket` (concept 2).
3. **The database enforces its own invariants.** `CHECK (status IN ('open',
   'resolved'))` and `CHECK (priority IN (...))` in [`db/init.sql`](../../db/init.sql)
   mean the state the model reads is always well-formed, whatever any caller
   attempted.
4. **Grounding is measured, not assumed.** The `grounding` evaluation suite
   checks the answer against what the tools actually returned (concept 5).

Step 1 is a request. Steps 2 to 4 are what make it hold.

## Measuring grounding deterministically

`grounding_scores` in [`evaluation/scorers.py`](../../evaluation/scorers.py)
compares the answer with the captured tool results:

* **Numbers are compared by value**, so `980.0` in a tool result grounds
  `$980.00` in the answer.
* **Forbidden assertions** come from the dataset: an answer about `TKT-1001`
  must not mention `TKT-9999`.
* **Grounding and completeness are scored separately**, and only grounding is
  gated. Inventing a value is an integrity failure. Omitting one is a
  thoroughness failure. Averaging them hides which one moved. See
  [EVALUATION.md](../EVALUATION.md#the-methodology-worth-reusing).

The `GROUND-ABSENT-RECORD` case (*"Tell me about ticket TKT-DOES-NOT-EXIST"*)
checks the most important grounding behaviour: the agent must *ask the system
of record* before saying anything about a ticket.

On the default `qwen3:8b`, in the evaluation run and in two direct runs, the
agent answered *"The ticket ID "TKT-DOES-NOT-EXIST" does not exist in the
system"* **without calling any tool**. It inferred that from the identifier's
name. The claim happens to be true, and it is still a hallucinated statement
about authoritative state: nothing was checked, and the same reasoning would
produce a confident "does not exist" for a real ticket with an unusual id. The
deterministic scorer caught it (`grounding_tool_used = False`), and the gate
went red. Contrast `TKT-9999`, where the model did call `get_ticket` and
reported the tool's "not found" error.

## Current state versus history

The schema separates *what is true now* from *what happened*:

* `tickets.priority` is the current state;
* `ticket_events` is the history the agent reads;
* `ticket_audit` (used by the optional approval feature) holds the committed
  decisions that produced state changes.

Reading one is never a substitute for reading the other. An agent that infers
"the priority was raised yesterday" by comparing current state with a remembered
earlier answer is reconstructing history from memory. That is the failure
grounding exists to prevent. See
[EXTENDING.md](../EXTENDING.md#the-patterns-worth-keeping-even-if-you-do-not-use-approvals).

## Grounded is not the same as correct

Grounding guarantees the *inputs* to the model's reasoning are real. It does
not guarantee the reasoning.

Observed while writing this material, on the default `qwen3:8b` at temperature
0, for *"Which ticket should we handle first, and why?"*:

* **On one agent build**, 3 of 3 runs made one correct
  `search_tickets(status="open")` call, whose result included `TKT-1002` with
  priority `urgent`, and then answered:

  > the ticket with the highest priority is **TKT-1004** (Duplicate charge) with
  > a priority of "high"

  Every fact in that sentence is grounded. The conclusion is wrong, because
  `urgent` outranks `high`. The answer passes a "did it use the tools" check and
  fails an "is it right" check.
* **On a rebuild from commit `af29ce0`**, with the same configuration digest and
  the same model, 4 of 4 runs fanned out to `get_ticket` for three tickets and
  then ended without an answer. That is the failure
  [CONFIGURATION.md](../CONFIGURATION.md#qwen38b-and-the-prioritization-prompt)
  records, along with a model (`qwen3.5:9b`) that answered correctly.

Both are wrong, in different ways, and nothing in the configuration changed
between them. [TEST-SCENARIOS.md](../TEST-SCENARIOS.md#6-prioritization-reasoning)
names TKT-1002 as the expected pick. The lesson about grounding is the first
bullet. The lesson about *probabilistic components* is the pair: re-evaluate
after every rebuild and dependency upgrade, not only after prompt or model
changes.

Engineering responses, roughly in order of reliability:

1. **Move deterministic logic out of the model.** Use the model for decisions
   that benefit from interpretation, and ordinary code for decisions that can
   be specified deterministically. If "highest priority, then oldest" is the
   business rule, compute it in SQL (`ORDER BY` a priority rank) and return it
   from a tool. The model then explains the ranking instead of
   performing it. This is the same move as the fan-out fix in
   [concept 2](02-tools-and-mcp.md#when-agent-problems-are-api-design-problems).
2. **Evaluate the decision, not just the grounding.** There is no evaluation case
   for prioritization today. Adding one is a [challenge](../CHALLENGES.md).
3. **Use a stronger model** for multi-step reasoning, and measure it with the same
   suites before switching.

## Provenance

Grounding answers *where did this fact come from?* for a single answer.
**Provenance** asks the same question of the system: *which agent, prompt and
model produced this result?*
[`evaluation/provenance.py`](../../evaluation/provenance.py) records the running
agent's own `/version` (prompt digests and model names), the prompt registry
version, and the harness commit with every evaluation result. It flags when they
disagree. A score you cannot attribute to a specific configuration is not
evidence. See [EVALUATION.md — provenance](../EVALUATION.md#provenance).

## On the Rig implementation

Grounding does not depend on the agent runtime: the same MCP tools return the
same authoritative rows to either agent, and the same deterministic grounding
scorers judge the answers. The Rig agent passes the grounding suite on the same
model, and its tool-result events carry the same fields the scorers read. One
detail differs: the *display copy* of a tool result shown in the UI and the
trace is redacted on Rig, while the model and the evaluator's grounding data
come from the raw result. → [EVALUATION.md](../EVALUATION.md#comparing-runtimes)

## Go deeper

* Lab: [04 — Break the agent](../tutorials/04-break-the-agent.md) (hallucinated
  state, weak vs stronger model), [05 — Evaluate the agent](../tutorials/05-evaluate-the-agent.md)
* Reference: [EVALUATION.md](../EVALUATION.md)
* Source: `grounding_scores` and `action_claims` in
  [`evaluation/scorers.py`](../../evaluation/scorers.py),
  [`evaluation/datasets/grounding.json`](../../evaluation/datasets/grounding.json)
* Next concept: [4. Guardrails and deterministic controls](04-guardrails-and-deterministic-controls.md)
