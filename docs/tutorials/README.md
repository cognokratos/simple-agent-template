# Labs

Hands-on exercises against the real template. Each lab builds on the previous
ones. Each one names the concept it teaches, has you change or observe the
running system, and ends with pointers into the reference documentation.

Start with the [request walkthrough](REQUEST-WALKTHROUGH.md) if you want the
end-to-end picture first.

| Lab | You will | Needs |
| --- | --- | --- |
| [01 — Run the agent](01-run-the-agent.md) | Start the stack, sign in, prove the agent refuses unauthenticated callers | Docker, a model endpoint |
| [02 — Understand tool calling](02-understand-tool-calling.md) | See the tool schemas the model gets and how descriptions steer it | 01 |
| [03 — Add an MCP tool](03-add-an-mcp-tool.md) | Add a typed, read-only, parameterized tool end to end | 02, Rust toolchain optional |
| [04 — Break the agent](04-break-the-agent.md) | Injection, tool-call explosion, weak model, hallucination | 01 |
| [05 — Evaluate the agent](05-evaluate-the-agent.md) | Run the suites, read the results, write a case the agent fails | 01 |
| [06 — Debug with traces](06-debug-with-traces.md) | Read a trace, find where time and decisions went | 01 |
| [07 — Experiment with guardrails](07-experiment-with-guardrails.md) | Watch each rail layer decide, and switch layers off | 01 |
| [08 — Add a state-changing action](08-add-a-state-changing-action.md) | See why a write tool is dangerous; add a backend policy rule | Rust toolchain |
| [09 — Add human approval](09-add-human-approval.md) | Enable the signed approval flow and audit a change | 01 |
| [10 — Build your own domain agent](10-build-your-own-domain-agent.md) | Replace the sample domain, keep the infrastructure | all |

Then try the [challenges](../CHALLENGES.md).

## Lab structure

Every lab uses the same sections: **Objective**, **Concept**, **Architecture
before**, **Exercise**, **Run it**, **Observe**, **Break it**, **Why it
failed**, **Architecture after**, **What you learned**, **Go deeper**.

## Ground rules

* **Break things locally, on a branch, and put them back.** No lab asks you to
  commit a weakened control. The checked-in default stays secure and read-only,
  and CI asserts that
  ([`verify_read_only_default.py`](../../scripts/verify_read_only_default.py)).
* **`agent/config.yml` is baked into the agent image.** After editing it, run
  `make rebuild-agent`. After editing the MCP server, run `make rebuild-mcp`
  (which also recreates the agent, since tools are discovered at startup).
  Environment-only changes in `.env` need `make up` to recreate the affected
  containers.
* **To undo:** `git checkout -- <file>` (or `git stash`), then the same rebuild
  target.
* **Model output varies.** Where a lab quotes model behaviour, it says which
  model and how many runs. Your results on another model, or another day, may
  differ. Finding out is part of the exercise.

## Observed results

The quoted results in labs 03–07 were recorded against this repository with the
default configuration: `qwen3:8b` as both agent and guard model on a local
Ollama, `temperature: 0.0`. They come from two agent builds with an identical
configuration digest: one built before, and one from, commit `af29ce0`. Where
the two builds behaved differently, the labs say so. Deterministic results (401s, rail blocks
driven by patterns, PII masking, the audit trigger, unit tests) were also
checked on that setup.
