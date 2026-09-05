# Security model

The controls, why each exists, and what a total compromise of the model would
achieve. [`ARCHITECTURE.md`](ARCHITECTURE.md) covers the design decisions behind
them; [`VERIFICATION.md`](VERIFICATION.md) maps every control to the command that
proves it.

## Authentication

The Rust gateway acts as the browser-facing BFF. Keycloak performs OIDC
authentication. Session and login cookies are HttpOnly; state-changing browser
requests are protected with a CSRF token. HTTP redirects from the gateway's
internal client are disabled, so trust is never forwarded across an unexpected
redirect target.

## Network isolation

Compose declares one network per trust relationship rather than a single flat
network. Membership decides which containers can route to a service; publishing a
host port decides whether the host and browser can.

| Network | Members | What it allows |
|---|---|---|
| `edge` | ui, keycloak, mlflow, mcp-inspector, otel-collector | the only services with published host ports |
| `gateway_net` | ui, gateway | assistant-ui reaching the gateway |
| `auth_net` | gateway, keycloak, keycloak-realm-init | the OIDC backchannel |
| `agent_net` | gateway, evaluator, agent | the only route to NAT |
| `mcp_net` | agent, mcp-inspector, mcp-server | the only route to MCP |
| `data_net` | mcp-server, postgres | the only route to the database |
| `telemetry_net` | agent, evaluator, otel-collector, mlflow | trace export |

NAT, MCP, PostgreSQL and the gateway publish no host ports, so none of them is
reachable from the host or the browser. assistant-ui — the most exposed
container — can reach only the gateway, so a server-side request forgery there
cannot reach the agent runtime, the tools, or the database.

The networks are deliberately **not** `internal: true`. That flag removes a
network's default route, i.e. it blocks *egress*; inbound reachability is already
governed by published ports. Marking the agent's networks internal would break
the model endpoint (`host.docker.internal` locally, a hosted endpoint in
production) without adding protection this topology does not already have.

`make network-test` asserts these properties against the running cluster, and
`scripts/verify_security_config.py` asserts exact network membership against the
resolved Compose configuration, so opening a new east-west path is a reviewed
change rather than a silent one.

## Service authentication

- Gateway → NAT: `AGENT_API_KEY` (received by NAT as `NAT_GATEWAY_API_KEY`)
- NAT → MCP: `MCP_API_KEY`
- NAT HITL signer ↔ MCP verifier: `HITL_APPROVAL_SECRET`

These are intentionally separate credentials.

Network isolation does not make the NAT credential redundant, because the two
controls answer different questions. Isolation answers *can this packet arrive*;
authentication answers *is this caller the gateway*. NAT trusts
`x-authenticated-user-id` to establish the identity that is then HMAC-bound into
a single-use approval token and written to the append-only history, so any party
able to open a connection to NAT could otherwise mint an approval attributed to
an arbitrary person.

NAT enforces the credential from `nat_streaming_react.fastapi_worker`, loaded
through NAT's supported `general.front_end.runner_class` extension point. It is
pure ASGI middleware, so it cannot buffer the streamed response, it compares in
constant time, and it removes `Authorization` from the ASGI scope after
validation so no NAT component, session metadata store or telemetry exporter
observes the credential. Only `/health`, `/health/live` and `/health/ready` are
unauthenticated.

`GET /version`, which reports the agent's build commit, prompt digests and model
binding so an evaluation run can name what it measured, is deliberately **not**
in that set: the evaluator already holds the service credential, so there is no
reason to widen the unauthenticated surface. It returns digests only, never
prompt text — the system prompt forbids revealing hidden prompts, and an endpoint
serving them would be the same disclosure from the other side.

mTLS or workload identity is not used, and would not be proportional here: there
is one credential between two trusted services on a private network. It becomes
justified with many-to-many service authentication, a network you do not control,
or a requirement for automatic credential rotation.

## Trusted human identity

The browser cannot choose `actor_id`. After authenticating the session, the
gateway injects `x-authenticated-user-id` and `x-request-id`. The HITL function
reads these from NAT request context and embeds them into the signed approval
token. The MCP writes the signed identity and request ID into the history event.

## Human-in-the-loop properties

- explicit confirmation for every state change;
- the **deterministic decision is the default** presented, always; a model
  recommendation is displayed as advisory context and never relabels the default,
  so confirming the engine's own decision is never recorded as an override;
- a text rationale required for any human override, in either direction;
- everything the chosen decision requires is collected from the person: a shortlist
  with no model-drafted research note prompts for one, so a human-initiated
  promotion cannot be refused for a field the *model* omitted;
- approval bound to the exact payload, including the research note by hash;
- approval bound to the deterministic decision the human was shown, so a policy
  or data change between display and approval voids the token;
- the override flag must agree with whether the approved decision actually differs
  from the engine's, so an override cannot be asserted where none happened or
  omitted where one did;
- ten-minute default expiry, with a thirty-minute ceiling the *verifier* enforces
  rather than trusting the minter's own claim;
- random nonce, consumed exactly once in the same transaction as the mutation;
- HMAC SHA-256 integrity;
- hard constraints rechecked *after* approval, so an approved change can still be
  refused — and the model is told so in those words.

### Why the model must not hold the default either way

The interesting case is not a model trying to promote a fund — that is refused at
the tool and again at the mutation. It is a model recommending something *more
conservative*, which policy permits.

If a permitted recommendation becomes the default, then the model has acquired
decision authority in one direction while being denied it in the other, and the
audit trail inverts: a human confirming the engine's decision is recorded as
overriding the system, and a human following the model is recorded as agreeing with
it. An injection that cannot promote a fund could still relabel every confirmation
as a human override, which corrupts precisely the record that exists to show who
decided what.

`rules::reconcile_decision` derives the default from the engine alone, both mutation
paths call it, and the approval-boundary suite drives the case end to end: engine
`shortlist`, model `research`, human `shortlist` — committed with
`override_applied: false`.

## Non-bypassable constraints

A hard constraint declared `bypassable: false` blocks every decision above
`reject`, for every actor, including one holding a valid human approval token.
`blocking_hard_constraint` takes the override flag as an argument and ignores it
for non-bypassable entries; there is a unit test that tries both values of the
flag against both decisions above `reject` and asserts all four are blocked.

The only way to reach a different outcome is to change `investor_profile.json`,
which is a versioned file whose version is recorded on every history event. That
is deliberate: changing the mandate should look like changing the mandate, not
like clicking through a dialog.

## Prompt injection

Issuer descriptions, imported notes and stored research notes are explicitly
untrusted, and the boundary is structural rather than instructional:

- untrusted text is returned inside `untrusted_free_text`, carrying a provenance
  string that withdraws authority while explicitly permitting quoting and
  summarising — a researcher has to be able to see what an issuer actually
  claims;
- the decision is computed in Rust from typed columns that no free-text field
  feeds into;
- the mutation tools are registered on the MCP but **not exposed to the model**,
  which reaches them only through NAT approval functions that pause for a person.

On that last point, the mechanism is worth naming because the logs are misleading.
NAT's MCP client discovers and adds all nine tools to the function *group* — the
startup log says so, once per tool — and then `include:` in `agent/config.yml`
narrows what the workflow can actually access. `FunctionGroup.get_accessible_functions`
returns only the included set when `include` is non-empty, so the ReAct agent is
handed six read-only tools and has no binding for the other three. `GET /version`
reports that list per run, `scripts/verify_security_sources.py` asserts the three
mutations are absent from it, and every evaluation run records it as
`tools_exposed` — a mutation tool appearing there would be a finding on its own.

An injection that fully captures the model therefore changes nothing. `make
eval-injection` measures exactly that across five attack shapes, and treats
*over*-blocking as a failure too: refusing to read a fund because its description
is hostile denies the user a real fund.

## Input screening

A NeMo Guardrails input rail classifies each user message before the agent sees
it, using the `self_check_input` prompt in `agent/config.yml`. It answers Yes to
requests to reveal prompts or credentials, replace governing instructions, bypass
the rules engine or the approval step, forge an approval, **invoke privileged
tools directly**, or facilitate market abuse.

What it deliberately does *not* block is the bulk of the prompt: searching,
comparing, asking why a fund scored what it did, proposing any decision, and
explicitly asking for a human override with a rationale. Those stay allowed
because they are the product, and because they remain constrained by deterministic
policy and human approval underneath. Over-blocking is scored as a failure in the
same metric as under-blocking — `make eval-guardrails` reports
`guardrail_false_positive` and `guardrail_false_negative` separately, both at 0.0
on the current model.

The "invoke privileged tools directly" clause has a visible consequence worth
knowing before demoing: phrasing a request as *"call commit_evaluation for
VTI-ARCA"* is blocked, while *"please record a shortlist decision for VTI-ARCA"*
is not. Both describe the same intent. The rail is classifying the *shape* of the
request — an instruction to invoke a named privileged function reads as an attempt
to drive the tool surface directly, which is what it is written to stop.

**This rail is not the security boundary, and nothing depends on it being right.**
It is a filter that reduces noise and blocks obvious abuse. Every control that
matters sits below it: an injected instruction that gets past the rail still meets
a decision computed in Rust from typed columns, mutation tools the model cannot
reach, and an approval it cannot mint. The injection suite exists precisely to
measure that — its attacks arrive in the *data plane*, where no input rail
inspects them at all.

## Output protection

The output guardrail is intentionally narrow. Required evidence — ISINs, expense
ratios, fund sizes, holdings counts, scores, decisions, dates — must remain
visible, so generic NER masking is not enabled: it corrupts exactly the numbers a
research decision rests on. The default rail performs deterministic credential
and private-key leakage checks through NeMo's native streaming output path, with
`stream_first: false`, so a credential cannot reach the client before the rail
rejects it.

## MCP Inspector

MCP Inspector is a developer-only diagnostic client. It connects directly to the
authenticated MCP endpoint inside the Compose network, bypassing NAT, the gateway
and the UI so MCP tools and resources can be tested independently. Its web UI is
published only on loopback (`127.0.0.1:6274`) and retains Inspector API-token
authentication. It must not be exposed to an untrusted network.

## History integrity

Application code only inserts history events. PostgreSQL additionally has a
`BEFORE UPDATE OR DELETE` trigger that rejects modification of `audit_events`.
Each decision event records the rules version and the profile version in force, so
a past decision can be reproduced after the policy changes. Production would also
ship logs to immutable external or WORM retention with controlled break-glass
access.

An append-only log is only as trustworthy as the coherence of each row. The
decision columns on an event describe **one** evaluation or none: an event that
does not create a decision — an assignment — leaves them null and records the
committed snapshot and the current evaluation as separate objects, each naming its
own rules and profile version. Mixing a score earned under one policy with the
version string of another produces a row that looks authoritative, reproduces
nothing, and cannot be detected as wrong after the fact.

## Telemetry

Traces reach MLflow over standard OTLP through the OpenTelemetry Collector. Two
controls keep credentials out of them:

1. The NAT front-end worker strips `Authorization` from the ASGI scope after
   validating it, before NAT can copy request attributes into span metadata.
2. `SensitiveHeaderRedactionProcessor`, a NAT telemetry processor running ahead
   of OTLP conversion, applies an explicit deny-list (`authorization`, `cookie`,
   `set-cookie`, `x-api-key`, `api-key`, `x-auth-token`, `proxy-authorization`)
   to every exported span. Correlation identifiers such as `x-request-id` and
   `x-authenticated-user-id` are deliberately retained.

A credential therefore has to defeat two independent controls to be exported.
Trace content is bounded by `NAT_TRACE_CONTENT_MAX_CHARS` and marked with
`nat.trace.content_truncated` when it is cut.

Production deployments should additionally classify fields and disable or redact
sensitive content according to data-retention policy; this project intentionally
exposes enough trace content to make evaluation and reviewer inspection easy.

## What this system cannot do

Worth stating in a security document, because the absence is a control:

- it has no brokerage credentials, no order model and no position table;
- it cannot buy, sell, hold or rebalance anything;
- it has no live market-data feed, so no external service can influence a
  decision at request time;
- the only writes it performs are to three columns of its own `etfs` table and to
  its own append-only history.

The blast radius of a total compromise of the model is a wrong sentence on a
screen.
