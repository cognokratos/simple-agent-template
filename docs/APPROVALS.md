# Human approval for state-changing actions

**Optional, and off in the shipped configuration.** The sample application is
read-only and the model has no capability to change state.

## Enabling it

All four are required; no single one opens the path:

1. uncomment the `functions:` block in `agent/config.yml`;
2. add `ticket_priority_change` to `workflow.tool_names`;
3. set `HITL_APPROVAL_SECRET` (≥ 24 characters, identical for the agent and the
   MCP server);
4. set `HITL_ENABLE_INTERACTIVE=true` so NAT mounts its interaction endpoints.

Without the secret the MCP server routes **no execution endpoint at all** — there
is no mutation surface rather than a disabled one. CI asserts the shipped
default is read-only.

## The flow

```
model calls the approval function with its proposal
  → NAT pauses the workflow and emits `event: interaction_required`
  → the UI renders an approval card in the thread
  → the human chooses; a change requires them to type a reason
  → the response is proxied: authenticated, CSRF-checked
  → the interaction guard checks ownership and the offered choice
  → the workflow resumes and mints a signed token
  → the MCP server verifies it and applies the change in one transaction
  → the model is told what happened; it never restates the payload
```

Authorization and effect are one step. Nothing between the human's confirmation
and the state change depends on further model output, so a request can never end
up approved but unapplied — and the model never gets an opportunity to alter
what was approved.

## Four layers, none trusted alone

| Layer | Checks | Does not check |
| --- | --- | --- |
| **Gateway** | shape, size, encoding, UUID form, protocol-level confirm/cancel consistency | which choices are legitimate — it cannot know, for an arbitrary application |
| **Interaction guard** | the responder owns the execution; the submitted id **and** value, together, are one *this* prompt actually offered as a pair; the response type matches the prompt type | anything about the resulting mutation |
| **Agent** | mints a token binding action, resource, actor, request, authoritative state, exact payload | nothing about current state — that has moved by the time it is applied |
| **MCP server** | signature, every binding, lifetime ceiling, re-derived state under a row lock, transition policy, single use | — |

### The gap the interaction guard closes

NAT's `POST /executions/{e}/interactions/{i}/response` calls
`ExecutionStore.resolve_interaction` and nothing else. It does not consider who
is asking, and `ExecutionRecord` carries no owner. In stock NAT, **knowing two
UUIDs is sufficient authority to answer somebody else's approval prompt**, with
any choice the schema permits.

`OwnerAwareExecutionStore` substitutes for NAT's store — a supported extension
point, since the worker assigns `self._execution_store` in `__init__` — and
checks both properties before resolution. Ownership is captured where each side
can see it: the prompt's actor from the workflow task's inherited contextvars,
the responder's from a pure-ASGI middleware on the response request.

An interaction this guard never saw created (NAT's OAuth consent flow) has no
recorded owner; those are allowed through and logged, because refusing them
would break a NAT feature. `HITL_STRICT_INTERACTION_OWNERSHIP=true` makes even
that case fail closed, for a deployment where approvals are the only interaction
type.

## The token

HMAC-SHA256 over a base64url claim set. Claims:

| Claim | Meaning |
| --- | --- |
| `action`, `resource_id` | what, to which record |
| `actor_id` | the authenticated human, from the gateway header — never the model |
| `request_id` | the one authenticated request this approval belongs to |
| `choice`, `expected_choice` | what the human picked, and the authoritative state they were shown |
| `override_requested` | recorded, **never trusted**: re-derived at the point of mutation |
| `rationale` | required for an override |
| `payload`, `payload_sha256` | application-owned fields, carried inside the signature |
| `exp`, `nonce` | lifetime and single-use identity |

The token **is** the payload. Every mutation parameter is read from the signed
claims rather than from tool arguments, so the model cannot alter, drop or
re-draft any part of what the human approved.

Every field that ends up in the signed claims — including `payload` fields
that originate with the model, like `note` — is displayed to the human,
labelled as model-supplied and not verified, in the same prompt where they
approve or cancel. The prompt-building code normalizes each such field exactly
once and reuses that value for display, signing and persistence, so what the
human read is provably what got signed: there is no second read of the raw
request that display and signing could disagree on. Signing content nobody
showed the approver would not be a human approval of it.

`expected_choice` is re-derived under a row lock at execution time. If the
resource or the policy moved under the approval, the token is void rather than
applied against a state nobody agreed to.

The minter caps its own TTL at 30 minutes, and the verifier enforces its own
independent ceiling — the minter is not the trust boundary. Expiry is strict;
the 60-second skew tolerance applies only to the lifetime ceiling, because
leniency on expiry would extend the window an approval stays spendable.

## Transactional integrity

One transaction, in this order:

1. consume the nonce (primary key, so a concurrent second spend conflicts);
2. lock the resource row and re-derive the authoritative state;
3. re-validate the transition against backend policy;
4. apply the mutation;
5. append the audit record.

Any failure rolls all of it back, **including the nonce**. That matters in both
directions: consuming first means two concurrent spends cannot both proceed, and
rolling back on failure means a refused approval is not silently burned. The
human's decision is either applied and recorded, or nothing happened at all.

A refusal is a `200` with `ok: false`, not an error. A legitimately approved
change can still be refused by policy, and the caller must be able to tell the
user plainly that nothing was applied. The model is told so explicitly —
reporting success either way is how an agent ends up telling a user a refused
change was applied.

## The audit trail

`ticket_audit` is append-only by **trigger**, not by convention. A decision record
that can be edited or deleted is not an audit trail.

* typed facts (`ticket_id`, `previous_priority`, `new_priority`, `actor_id`,
  `request_id`, `nonce`) are structurally separate from untrusted free text
  (`rationale`, `payload`), so the boundary is visible in the schema;
* `policy_context` records the policy version in force when the decision was
  taken, so an old row stays interpretable after the rules change;
* `tickets.priority` is the current evaluation and these rows are the committed
  decisions that produced it. Reading one is never a substitute for the other.

## Generalizing it

| Template | Yours |
| --- | --- |
| `resource_id` | any identifier |
| `set_ticket_priority` | your action, in `mutation::ACTIONS` |
| `low` / `medium` / `high` / `urgent` | your `allowed_choices` |
| `payload.note` | your payload fields |

Adding an action: a request model and a registered function in
`agent/src/nat_streaming_react/approval.py`, an entry in `mutation::ACTIONS` on
the MCP side, and the mutation itself. Nothing in the token format or the
verification changes.

The action registry is a fixed list rather than configuration: the set of things
a human can authorize is a security property of the deployment.

## Verifying it

```
make verify-approvals        # 29 agent-side checks, offline
make verify-approvals-rust   # 25 MCP-side checks
```

Between them: forged and tampered tokens, expiry, the lifetime ceiling and its
skew tolerance, wrong action/resource/request, moved authoritative state,
payload-digest disagreement, missing identity, replay, cancellation, invalid and
unoffered choices, unauthorized interaction responses, every transition rule, and
that a token minted by the Python agent is accepted by the Rust verifier —
including a non-ASCII payload, which proves the two canonical JSON encoders
agree.

## What is not covered

Replay and rollback are tested at the level of the policy and the verifier.
The transactional behaviour itself — nonce conflict under concurrency, rollback
on a failed audit insert — is enforced by the database and is **not** covered by
an automated test in this template, because it needs a live PostgreSQL. See
[LIMITATIONS.md](LIMITATIONS.md).
