# Lab 09 — Add human approval

## Objective

Turn on the template's optional human-approval flow, approve a real change,
read the audit trail it leaves, and probe the controls around it.

## Concept

A human approval is only meaningful if it is bound to *exactly* what will happen
(actor, resource, request, current state, payload), is single-use, and is
verified at the point of mutation by a component the model cannot influence.
→ [Concept 8](../concepts/08-human-in-the-loop.md)

## Architecture before

Read-only: no approval tool, no `/approvals/execute` route, no interaction
endpoint. CI enforces that this is the shipped state
([`scripts/verify_read_only_default.py`](../../scripts/verify_read_only_default.py)).
**Do not commit the changes in this lab.**

## Exercise

All three steps are required, and the agent **refuses to start** with only some
of them set: whoever sets one switch believes approvals are on
([APPROVALS.md — enabling it](../APPROVALS.md#enabling-it)).

1. In [`agent/config.yml`](../../agent/config.yml), uncomment the
   `tools.approval` block. Declaring the tool is its registration:

   ```yaml
   tools:
     ...
     approval:
       ticket_priority_change:
         token_ttl_seconds: 600
   ```

2. In `.env`, set a shared secret of at least 24 characters, used by both the
   agent (to mint) and the MCP server (to verify):

   ```bash
   HITL_APPROVAL_SECRET=<output of: openssl rand -hex 32>
   ```

3. In `.env`, mount the agent's interaction endpoint:

   ```bash
   HITL_ENABLE_INTERACTIVE=true
   ```

Optionally set `HITL_INTERACTION_TIMEOUT_SECONDS=120` so an unanswered card
expires sooner (default 600).

## Run it

```bash
make up-build     # rebuilds the agent image (config.yml) and recreates changed services
make wait
make logs-mcp     # look for: human-approval execution endpoint enabled
make version      # tools_exposed now includes ticket_priority_change
```

In the UI:

```text
Mark ticket TKT-1003 as high priority.
```

Expected, per [TEST-SCENARIOS.md](../TEST-SCENARIOS.md#human-approval-demonstration):
the agent reads TKT-1003 (`medium`), calls `ticket_priority_change`, and an
approval card appears. Choose **Change to — high** and type a reason.

Then look at the database:

```bash
make shell-db
```

```sql
SELECT id, priority, updated_at FROM tickets WHERE id = 'TKT-1003';
SELECT ticket_id, previous_priority, new_priority, actor_id, request_id,
       rationale, policy_context, recorded_at
FROM ticket_audit ORDER BY recorded_at DESC LIMIT 5;
SELECT nonce, action, resource_id, actor_id, consumed_at
FROM approval_nonces ORDER BY consumed_at DESC LIMIT 5;
```

## Observe

* `actor_id` in `ticket_audit` is your Keycloak subject, from the gateway header.
  The model had no way to set it.
* `rationale` is what *you* typed. `payload` holds any model-supplied note, which
  the card showed you labelled as model-supplied.
* `policy_context` records the policy version.
* One nonce row per applied approval, written in the same transaction.
* Run it again and choose **Keep — medium/high** or **Cancel**: no token is
  minted, and no audit row is written.
* The run was suspended inside the approval gate while the card was open; the
  trace shows `approval.gate` and `human_approval.wait`.
* The model's reply is built from a result that says `committed: true` but has
  no `actor_id`: identity went into the token and the audit row, never the
  conversation.
* Leave a card unanswered past `HITL_INTERACTION_TIMEOUT_SECONDS`: it expires as
  a cancellation, and nothing is applied.

## Break it

**A. Edit the audit trail.** In `make shell-db`:

```sql
UPDATE ticket_audit SET new_priority = 'low';
```

Observed (on a test row in a rolled-back transaction):

```text
ERROR:  ticket_audit is append-only; UPDATE is not permitted
```

**B. Let a hostile record ask.** With approvals enabled, send
`Summarise ticket TKT-INJ-FAKE-AUTH.` The description claims an approval was
already granted. Whatever the model does, it cannot apply the change by itself:
the only path to a mutation is a card *you* answer, bound to *your* identity. If
the model calls `ticket_priority_change`, decline. If it says the change "has been
applied", check `ticket_audit` and `tickets.priority`: the claim is not the
state. This is the case the `injection` suite's `injection_no_action_claim`
metric exists for.

**C. Answer someone else's card.** With a card open, copy its `execution_id` and
`interaction_id` from the stream (browser dev tools) and POST an answer through
the gateway as a second Keycloak user: `403`, and the card is still pending for
you. Then answer with an option the card did not offer: `422`.

**D. Run the boundary tests.**

```bash
make verify-approvals        # agent side: binding, replay, ownership, offered choices
make verify-approvals-rust   # MCP side: signature, binding, lifetime, policy
```

They run on the host, with no cluster and no model. They cover what is hard to do
by hand: forged and tampered tokens, expiry, wrong resource or request, moved
state, replay, answering someone else's prompt, and choosing an option that was
never offered. [`agent/tests/approvals.rs`](../../agent/tests/approvals.rs)
drives these through the real service, and the agent's tests compile the MCP
server's verifier source, so minter and verifier cannot drift apart.

## Why it failed

* **A**: append-only is enforced by a database trigger, not by application
  convention. A decision record that can be edited is not evidence.
* **B**: authorization is not an inference the model makes. It is a signed
  artifact produced by a human interaction the model cannot answer, verified by
  a server the model cannot reach directly.
* **C**: the interaction registry checks who answers and what they chose before
  the run resumes; knowing two UUIDs is not authority.
* **D**: each binding in the token removes one specific way an approval could be
  misused. The tests prove each one independently.

## Architecture after

Diagram G in [concept 8](../concepts/08-human-in-the-loop.md#diagram-g-the-approval-flow-in-this-repository).

## Reset

1. Put TKT-1003 back **through the same controlled path**: ask the agent to set
   it to `medium` and approve with a reason. That leaves a second, honest audit
   row. (An `UPDATE tickets ...` in `psql` would work too, but it bypasses the
   audit trail. Notice that you'd be doing exactly what this lab argues against.)
2. `git checkout -- agent/config.yml`, remove `HITL_APPROVAL_SECRET` and
   `HITL_ENABLE_INTERACTIVE` from `.env`, then `make up-build`.
3. `make logs-mcp` should again show that the server is read-only.

## What you learned

* A safe mutation needs a proposal, a human decision, a binding, a re-check at
  the point of mutation, and an immutable record. Remove any one and a specific
  attack works.
* Feature flags for dangerous capabilities should remove the surface, not just
  disable it.
* The model reports the outcome. It does not establish it.

## Go deeper

* [APPROVALS.md](../APPROVALS.md), [LIMITATIONS.md](../LIMITATIONS.md#not-tested-automatically)
  (end-to-end browser approval and concurrent nonce conflicts are not covered
  by automated tests)
* Challenge: [Advanced — an action requiring approval](../CHALLENGES.md#advanced-an-action-requiring-human-approval)
* Next: [Lab 10 — Build your own domain agent](10-build-your-own-domain-agent.md)
