# Building a domain application on this template

The support-tickets domain is a sample. This is what is yours to replace and
what is meant to be inherited unchanged.

## What is the sample

| File | Replace with |
| --- | --- |
| `db/init.sql` | your schema and seed data |
| `mcp-server/src/main.rs` tool functions | your read-only tools |
| NAT (`main`): `agent/config.yml` — `system_prompt`, `tool_names`, `include`, rail prompts, allow templates | your prompt and tool surface |
| Rig (`rust-agent`): `agent/config.yml` — `workflow.system_prompt`, `tools.mcp.include` and `overrides`, rail prompt, output patterns and PII entities; `agent/src/guardrails/input.rs` — `CRITICAL_PATTERNS`, `ALLOW_TEMPLATES` | the same, in the Rig agent's layout |
| `evaluation/datasets/*.json` | your cases |
| `db/*_test_fixtures.sql` | your guardrail and injection fixtures |
| `ui/app/page.tsx` welcome copy | your examples |

## What is infrastructure

Everything else, and in particular:

* the gateway, entirely — OIDC, sessions, CSRF, proxying, stream limits;
* `fastapi_worker.py`, `interaction_guard.py`, `llm_config.py`,
  `guardrails_compat.py`, `observability/`, `provenance.py` (NAT); on Rig, the
  agent crate apart from the sample tables above;
* the approval token format and both verifiers;
* the evaluation harness, scorers and provenance;
* the Compose topology and its checks.

## What each local module compensates for

None of these are preferences. Each exists because NAT or NeMo Guardrails does
something specific that this deployment cannot use as shipped, and each names
the upstream change that would let it be deleted. Checked against NAT 1.9.0:
**every one still applies**, which is why upgrading from 1.8 removed none of
them.

| Module | Upstream behaviour it compensates for | Delete when |
| --- | --- | --- |
| `register.py` | NAT's ReAct `_stream_fn` buffers tokens until it sees the literal `Final Answer:`. With native tool calling the model returns a normal assistant message instead, so the fallback emits the entire answer as one chunk — no streaming. | The ReAct stream handles native tool calling without the marker |
| `text_guardrails.py` | NAT's `GuardrailsMiddleware` converts each streamed item with `str(chunk)`. For a `ChatResponseChunk` that serialises the whole Pydantic object instead of the assistant text, so the rails see JSON rather than prose. | The middleware extracts chat content rather than stringifying the chunk |
| `guardrails_compat.py` | Three streaming-rail defects in nemoguardrails 0.21 (see [GUARDRAILS.md](GUARDRAILS.md)). Fixed upstream in 0.23.0, which the `nvidia-nat-security[guardrails]` pin forbids. | That pin allows `>=0.23` |
| `interaction_guard.py` | NAT's interaction-response route authorizes on knowledge of two UUIDs. `ExecutionRecord` carries no owner, so any authenticated caller can answer anyone's approval prompt, with any choice the schema permits. | `ExecutionStore` records an owner and the route checks it |
| `llm_config.py` | NAT's YAML interpolation cannot express *absence*. An optional pass-through parameter such as `reasoning_effort` must be present for one provider and entirely absent for another; `${VAR:-}` always produces a string. | A configured-empty extra is omitted rather than forwarded |
| `observability/` | NAT exposes no public way to supply the workflow root span id or read the span-attribute prefix, so Guardrails spans would form a second trace. Three private attributes, each listed with its own condition in [OBSERVABILITY.md](OBSERVABILITY.md). | Those three have public equivalents |
| `fastapi_worker.py` | Partly not a workaround — `runner_class` is a supported extension point, and the service-credential layer exists because NAT *trusts* gateway-injected identity headers. `RequireIdentityHeaderMiddleware` **is** a workaround: NAT 1.9's `identity_header` raises `IdentityHeaderError`, but its interactive runner (used unconditionally for the workflow routes) catches it into a 200 response body, so the refusal never reaches the client. | The credential layer: never. The identity layer: when NAT's refusal produces a real 401 on the workflow routes |

Before assuming a module is obsolete after an upgrade, check the actual
behaviour rather than the release notes: for 1.9, four of the relevant upstream
files were byte-identical to 1.8.

### On the Rig implementation: what the agent modules are for

The Rig agent on `rust-agent` has no such workarounds: nothing patches, wraps or
reaches into Rig's internals, and no private Rig API is used. Each module is a
deliberate part of the design, and the NAT module it corresponds to is noted:

| Module (`agent/src/`) | What it is | NAT counterpart |
| --- | --- | --- |
| [`agent/execution.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/agent/execution.rs), [`agent/builder.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/agent/builder.rs) | the request lifecycle; a per-request Rig agent whose tools close over the trusted scope | `register.py` |
| [`agent/hooks.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/agent/hooks.rs), [`guardrails/tools.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/guardrails/tools.rs), [`mcp/schema.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/mcp/schema.rs) | deterministic tool policy at Rig's dispatch hook; strict schemas | none: tools were NAT configuration |
| [`guardrails/input.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/guardrails/input.rs), [`agent/input_rail.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/agent/input_rail.rs) | input policy rules and their runner | `text_guardrails.py` (input) |
| [`guardrails/output.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/guardrails/output.rs), [`guardrails/pii.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/guardrails/pii.rs) | streaming output policy | `text_guardrails.py` (output), `guardrails_compat.py` |
| [`approval/pending.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/approval/pending.rs) | interaction registry with ownership and offered-choice checks built in | `interaction_guard.py` |
| [`api/auth.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/api/auth.rs), [`identity.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/identity.rs) | service key and asserted identity, in that order | `fastapi_worker.py` |
| [`config.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/config.rs), [`agent/model.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/agent/model.rs) | typed, fail-fast configuration; empty means omitted | `llm_config.py` |
| [`telemetry/`](https://github.com/cognokratos/simple-agent-template/tree/rust-agent/agent/src/telemetry) | one tracing model, fixed export filter, provenance | `observability/`, `provenance.py` |

What `rust-agent` tracks upstream instead is Rig's public API, which moves
between minor versions ([RUST-BRANCH-MAINTENANCE.md](RUST-BRANCH-MAINTENANCE.md#upgrading-rig)).

## Recommended sequence

1. **Fork and rename.** Set `COMPOSE_PROJECT_NAME` so both stacks coexist.
   Rename the crates and the Keycloak realm/client if you want them branded;
   nothing depends on the names beyond the defaults in `docker-compose.yml`.
2. **Replace the schema and the MCP tools.** Keep them read-only at first.
3. **Rewrite the prompt and the tool list** in `agent/config.yml`. (Rig: the
   file is baked into the image — `make rebuild-agent`; a tool schema outside
   the subset the agent enforces stops startup with the reason.)
4. **Rewrite the guardrail input policy.** The self-check prompt and the
   critical patterns (NAT: `_CRITICAL_INPUT_PATTERNS`; Rig: `CRITICAL_PATTERNS`
   in `agent/src/guardrails/input.rs`) are domain judgements. The read-only allow
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
   * NAT: a request model and a `@register_function` in `approval.py`,
     following `ticket_set_priority_approval`;
   * Rig: a proposal schema and a gate in `agent/src/approval/` (as `mod.rs` and
     `gate.rs` do), registered as a `ToolSpec` with
     `ToolEffect::MutationRequiringApproval` in `agent/src/services.rs` and as a
     tool in `agent/src/agent/builder.rs`; the tool policy then routes it into
     the gate and nowhere else.
3. **Config** — NAT: declare the function and add it to `tool_names`; Rig:
   declare it under `tools.approval` (and in `ApprovalToolsConfig`).
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
