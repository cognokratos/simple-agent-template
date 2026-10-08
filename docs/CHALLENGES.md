# Challenges

Exercises without step-by-step solutions. Each is a competency milestone: if
you can complete it so that it meets every requirement, and explain why each
requirement exists, you have the skill.

Do them on a branch. None should weaken the checked-in default: CI must still
pass, and the shipped configuration must stay read-only.

## Beginner: a new read-only tool

Add a read-only MCP tool of your choice (not the one from
[lab 03](tutorials/03-add-an-mcp-tool.md)). Suggestions: tickets by assignee,
counts by status and priority, tickets touching one order reference.

Requirements:

- [ ] typed input struct with schema descriptions
- [ ] every argument validated; numeric limits clamped
- [ ] parameterized SQL only
- [ ] listed in `include:` and discoverable by the agent (`Adding tool ... to group` in `make logs-agent`)
- [ ] at least one evaluation case in `evaluation/datasets/tool_calling.json`, passing on your model
- [ ] `EVALUATION_TOOL_NAMES` updated
- [ ] a trace showing the call as a `tickets_mcp__<name>` span
- [ ] `cargo clippy --all-targets -- -D warnings` and `cargo test` clean

## Intermediate: a new entity and related tools

Add a new domain entity, for example `orders` (tickets already carry
`order_reference`) or `customers`, with a foreign-key relationship to tickets,
seed data, and two or three tools that let the agent answer questions spanning
both entities.

Requirements:

- [ ] schema with `CHECK` constraints for every enumerated field, and foreign keys
- [ ] tools sized so the common cross-entity questions need **at most two** tool calls (justify your granularity)
- [ ] system-prompt and `tool_overrides` guidance for when to use which tool
- [ ] `tools` and `grounding` dataset cases covering the new questions, including an absent-record case
- [ ] an injection fixture in the new entity's free-text field, and an `injection` case for it
- [ ] the read-only allow templates updated so that routine questions about the new entity are not blocked by the input rail

## Advanced: an action requiring human approval

Add a second approval-gated action, for example `assign_owner` or
`set_ticket_status` (`open` → `resolved`), following
[EXTENDING.md — adding an approval-gated action](EXTENDING.md#adding-an-approval-gated-action).

Requirements:

- [ ] an entry in `mutation::ACTIONS` with an explicit `allowed_choices`
- [ ] `apply_policy` rules for the action, each with a unit test (including at least one refusal)
- [ ] `apply` performs the mutation and an audit insert in the caller's transaction
- [ ] an agent-side approval step that takes identity from the trusted request context and signs the exact payload shown to the human (NAT: a registered function in `approval.py`; Rig: a gate in `agent/src/approval/`)
- [ ] `POLICY_VERSION` bumped
- [ ] `make verify-approvals-rust` and `make verify-approvals` pass
- [ ] an `injection` dataset case where stored text claims your action was already approved, scored by `injection_no_action_claim`
- [ ] the shipped configuration still read-only: with `.env` copied from `.env.example`, `docker compose config --format json | python3 scripts/verify_read_only_default.py` passes (this is what CI runs)

## Expert: replace the domain

Replace the support-ticket domain with a completely different one while
preserving the infrastructure. See [lab 10](tutorials/10-build-your-own-domain-agent.md).

Requirements:

- [ ] no changes to `gateway/`, the agent's authentication, interaction and observability modules (NAT: `fastapi_worker.py`, `interaction_guard.py`, `observability/`; Rig: `api/auth.rs`, `approval/pending.rs`, `telemetry/`), or `evaluation/scorers.py`. Justify any you could not avoid.
- [ ] all four evaluation suites with new datasets, passing their gates on your chosen model, with consistent provenance
- [ ] domain-specific input policy (self-check prompt, critical patterns, anchored allow templates) and `make verify-input-guardrails` passing
- [ ] injection fixtures and an `injection` suite for your domain's free-text fields
- [ ] `make static-check`, `make test` and CI green
- [ ] a short write-up of the deterministic controls protecting your domain's worst-case model failure

## Open problems from the labs

The labs surfaced real gaps. Each is a good self-directed project:

- **A decision-quality metric.** Lab 04 showed `qwen3:8b` picking the wrong
  ticket with fully grounded facts, and lab 05 showed that no current gate
  catches it. Design a scorer and gate for "picked the right ticket". Decide
  whether the ranking rule should instead move into a tool.
- **Repeated injected authority.** The default model restated a fabricated
  approval as fact, and the `injection` scorer (correctly, by its definition)
  passed it. Should that fail? If so, write a scorer that detects it without
  penalising faithful quotation. `action_claims`' quotation handling in
  [`evaluation/scorers.py`](../evaluation/scorers.py) is the place to start.
- **Server-side fan-out.** Replace the model-driven `search_tickets` + N×`get_ticket`
  pattern for "history for all open tickets" with a single bounded tool, and show
  the latency and `TOOLS-FANOUT-OPEN-TICKETS` results before and after.
- **Tool-span redaction.** Raw tool results reach the trace store (lab 06). Add
  redaction for configured fields in tool spans, without breaking the
  evaluation harness's ability to read tool results.
- **Per-user data authorization.** Pass the authenticated identity to the MCP
  server out of band (never as a model-chosen argument) and enforce row-level
  access in SQL. [LIMITATIONS.md](LIMITATIONS.md#before-production) lists this
  as a production prerequisite.
