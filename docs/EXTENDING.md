# Building a domain application on this template

The support-tickets domain is a sample. This is what is yours to replace and
what is meant to be inherited unchanged.

## What is the sample

| File | Replace with |
| --- | --- |
| `db/init.sql` | your schema and seed data |
| `mcp-server/src/main.rs` tool functions | your read-only tools |
| `agent/config.yml` — `workflow.system_prompt`, `tools.mcp.include` and `overrides`, `guardrails.prompts`, `guardrails.output` secret patterns and PII entities | your prompt, tool surface and rail policy |
| `agent/src/guardrails/input.rs` — `CRITICAL_PATTERNS`, `ALLOW_TEMPLATES` | your deterministic input policy |
| `evaluation/datasets/*.json` | your cases |
| `db/*_test_fixtures.sql` | your guardrail and injection fixtures |
| `ui/app/page.tsx` welcome copy | your examples |

## What is infrastructure

Everything else, and in particular:

* the gateway, entirely — OIDC, sessions, CSRF, proxying, stream limits;
* the agent crate apart from the sample entries above — see
  [what the agent modules are for](#what-the-agent-modules-are-for);
* the approval token format and both verifiers;
* the evaluation harness, scorers and provenance;
* the Compose topology and its checks.

## What the agent modules are for

None of these are workarounds. Nothing patches, wraps or reaches into Rig's
internals, and no private Rig API is used: the agent owns its request
lifecycle, and Rig supplies the model client, the tool-calling loop, MCP tool
adapters and the dispatch hook the policy runs in. Each module is a deliberate
part of the design. Most of them are where deterministic software keeps
authority the model does not have — identity, tool access, approvals, state
transitions — so they are infrastructure, not sample.

| Module (`agent/src/`) | What it is |
| --- | --- |
| [`agent/execution.rs`](../agent/src/agent/execution.rs), [`agent/builder.rs`](../agent/src/agent/builder.rs) | the request lifecycle; a per-request Rig agent whose tools close over the trusted scope |
| [`agent/state.rs`](../agent/src/agent/state.rs) | the execution state machine; an illegal transition is an error, not a silent state change |
| [`agent/hooks.rs`](../agent/src/agent/hooks.rs), [`guardrails/tools.rs`](../agent/src/guardrails/tools.rs), [`mcp/schema.rs`](../agent/src/mcp/schema.rs) | deterministic tool policy (`ToolPolicy::decide`) at Rig's dispatch hook; strict schemas; an exact tool-call budget |
| [`mcp/`](../agent/src/mcp) | MCP discovery against the allow-list in `agent/config.yml`; tools that are destructive, identity-bearing or unenforceable are refused at startup |
| [`guardrails/input.rs`](../agent/src/guardrails/input.rs), [`agent/input_rail.rs`](../agent/src/agent/input_rail.rs) | input policy rules and their runner, which fails closed |
| [`guardrails/output.rs`](../agent/src/guardrails/output.rs), [`guardrails/pii.rs`](../agent/src/guardrails/pii.rs) | streaming output policy: a bounded hold-back window, secret patterns, deterministic PII recognisers |
| [`approval/`](../agent/src/approval) | the approval gate, the interaction registry with ownership and offered-choice checks, token minting and execution against MCP |
| [`api/auth.rs`](../agent/src/api/auth.rs), [`identity.rs`](../agent/src/identity.rs) | service key and asserted identity, in that order; `TrustedCaller` never enters the model context |
| [`config.rs`](../agent/src/config.rs), [`agent/model.rs`](../agent/src/agent/model.rs) | typed, fail-fast configuration; empty means omitted |
| [`telemetry/`](../agent/src/telemetry) | one tracing model, fixed export filter, `/version` provenance |

What the agent tracks upstream is Rig's public API, which moves between minor
versions; the crates are pinned exactly
([AGENT-INTERNALS.md](AGENT-INTERNALS.md#upgrading-rig)). The retired NAT agent
needed several modules to work around specific toolkit behaviour; they are
described at the [`nat-1.9-final`](https://github.com/cognokratos/simple-agent-template/blob/nat-1.9-final/docs/EXTENDING.md#what-each-local-module-compensates-for)
tag and compared in [FRAMEWORK-MIGRATION.md](FRAMEWORK-MIGRATION.md).

## Recommended sequence

1. **Fork and rename.** Set `COMPOSE_PROJECT_NAME` so both stacks coexist.
   Rename the crates and the Keycloak realm/client if you want them branded;
   nothing depends on the names beyond the defaults in `docker-compose.yml`.
2. **Replace the schema and the MCP tools.** Keep them read-only at first.
3. **Rewrite the prompt and the tool list** in `agent/config.yml`. The file is
   baked into the image, so apply it with `make rebuild-agent`; a tool schema
   outside the subset the agent enforces stops startup with the reason.
4. **Rewrite the guardrail input policy.** The self-check prompt and the
   critical patterns (`CRITICAL_PATTERNS` in `agent/src/guardrails/input.rs`)
   are domain judgements. The read-only allow
   templates exist to correct LLM false positives on *your* common queries —
   anchor them to the complete message, as the samples are.
5. **Point the evaluator at your tools:** `EVALUATION_TOOL_NAMES`, new datasets,
   new experiment names. The scorers need no change; domain vocabulary goes in
   the dataset.
6. **Only then**, if you need mutations, enable approvals.

## Adding an approval-gated action

Four edits, and nothing in the token format or verification changes:

1. **MCP** — add to `mutation::ACTIONS`:
   ```rust
   Action { name: "assign_owner", carries_choice: true, allowed_choices: &["queue-a", "queue-b"] }
   ```
   and extend `apply_policy` / `apply` for it. The registry is a fixed list on
   purpose: what a human can authorize is a security property, not configuration.
2. **Agent** — prompt, collect a rationale when the choice differs from the
   authoritative state, mint, apply:
   a proposal schema and a gate in `agent/src/approval/` (as `mod.rs` and
   `gate.rs` do), registered as a `ToolSpec` with
   `ToolEffect::MutationRequiringApproval` in `agent/src/services.rs` and as a
   tool in `agent/src/agent/builder.rs`; the tool policy then routes it into
   the gate and nowhere else.
3. **Config** — declare it under `tools.approval` in `agent/config.yml` (and in
   `ApprovalToolsConfig` in `agent/src/config.rs`).
4. **Schema** — whatever the mutation and its audit record need.

The UI needs no change: the approval card renders whatever options the prompt
carries.

## The patterns worth keeping even if you do not use approvals

These are the reusable shapes, stated as interfaces rather than as a framework.
The template implements each one concretely in the approval path; if your
application has no mutations, the second and third still apply to anything that
writes.

**Model advice is distinct from authoritative policy.** A model recommendation is
recorded as advisory context and never becomes the default. In the sample, the
default offered to the human is always the current authoritative state. Give the
model a field to record its opinion in; do not let that field move a decision.

**Backend policy is validated at the point of mutation**, after the human
approves, under a lock on the row being changed — not when the prompt is built.
The world moves between the two, and a refusal at the point of mutation is the
control working. Report it as a refusal; never as a change that happened.

**State transitions are checked explicitly**, rather than inferred from a write
that would happen to succeed. `apply_policy` in `mutation.rs` is the shape: a
pure function of (action, claims, current state) returning permitted-or-why-not,
so the whole matrix is unit-testable without a database.

**Current evaluation is separate from committed history.** `tickets.priority` is
what is true now; `ticket_audit` rows are the decisions that produced it. Reading
one is never a substitute for the other. Do not reconstruct history by diffing
current state.

**Audit records carry a versioned policy context**, so a row stays interpretable
after the rules change. `policy_context` holds the policy version in force when
the decision was taken.

**Audit tables are append-only by enforcement**, not by convention — a trigger,
or a role that lacks `UPDATE`/`DELETE`. A decision record that can be edited is
not an audit trail.

**Typed facts and untrusted free text are structurally separate.** In the schema,
`actor_id` and `new_priority` are columns; `rationale` and `payload` are clearly
marked as human- or application-supplied text that is never interpreted as
instruction. Keeping the boundary visible in the schema is what stops it eroding.

**Source data carries provenance labels.** Where a record's field comes from an
external feed or a human note, say so in the row, so an answer can attribute it.
The template demonstrates this at the run level rather than the row level (see
`evaluation/provenance.py`); the same discipline applies to data.

A general policy engine is deliberately **not** provided. `Action` plus
`apply_policy` is the whole interface, and a domain's rules, weights and decision
bands belong in the domain.

## What not to inherit

* the tickets schema, seed rows and fixtures;
* the ticket-specific prompt, rail prompts and allow templates;
* the evaluation datasets;
* `EVALUATION_TOOL_NAMES` and the experiment names.
