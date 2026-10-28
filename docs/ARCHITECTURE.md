# Architecture and trust boundaries

A template for a secured, observable, evaluated LLM agent. The sample
application triages customer-support tickets; everything that is not the
sample is meant to be reused unchanged.

## Overview

![simple-agent-template production AI agent architecture](assets/simple-agent-template-architecture.svg)

The diagram is intentionally organized around one architectural principle:
**deterministic controls surround a probabilistic model**. Identity,
authorization, tool access, database access, approvals and audit are enforced by
software outside the model; the LLM is allowed to reason and choose tools only
inside those boundaries.

The LLM is the only probabilistic component. It is reached only from inside the
agent, sees no credentials or identity, and can affect state only through the
MCP tools. The sections below give the same picture as text, with the question
each boundary answers.

## The request path

```
browser
  │  session cookie + CSRF header, same-origin only
  ▼
assistant-ui (Next.js)            publishes :3000
  │  server-side fetch, no browser headers forwarded
  ▼
Rust gateway (BFF)                publishes nothing
  │  service credential + gateway-minted identity headers
  ▼
Agent runtime                     publishes nothing
  │  service credential
  ▼
Rust MCP server                   publishes nothing
  │
  ▼
PostgreSQL                        publishes nothing
```

Alongside it:

```
agent  ──OTLP──▶  OpenTelemetry Collector  ──▶  Phoenix            (default)
                                            └┄┄▶  MLflow             (optional, make dev-mlflow)

evaluator ──▶  agent (service credential, bypassing the browser path)
    │
    └──▶ evaluation/results/*.json   the result and the gate
            └──▶ copy to Phoenix (and MLflow, if enabled)   display only
```

## Inside the agent box

The agent is a Rust service built on the [Rig](https://github.com/0xPlaygrounds/rig)
agent framework. Rig runs the loop — model call, tool call, result, next model
call. Everything that decides authority is ordinary, tested Rust around it:

```text
Agent runtime (agent/)
  Axum router            service credential, then gateway-minted identity
  input policy           deterministic patterns + a self-check classifier call;
                         fails closed
  Rig agent loop         built per request; bounded by an exact tool-call budget
    + tool-policy hook   one deterministic function decides every proposed call
  MCP client             only the allow-listed tools are advertised or callable
  output policy          credential patterns and PII recognisers over the stream
```

**The framework is not the architecture.** The boundaries on this page —
identity, the tool allow-list, approvals, network segmentation — sit outside the
loop, and the agent satisfies a framework-neutral
[agent-service contract](AGENT-SERVICE-CONTRACT.md). That is what let the agent
be moved from NVIDIA NeMo Agent Toolkit to Rig without the UI, the gateway, the
MCP server or the evaluation suites changing; see
[FRAMEWORK-MIGRATION.md](FRAMEWORK-MIGRATION.md). Frameworks are replaceable;
trust boundaries are architecture.

Three boundaries in particular belong to the repository, not to any framework:

* **MCP is the capability boundary.** The model can reach state only through
  tools the MCP server exposes and `agent/config.yml` allow-lists
  (`tools.mcp.include`). The MCP server authenticates the agent, owns the
  database credential and re-checks policy at the point of mutation.
* **OpenTelemetry is the telemetry boundary.** The agent exports spans over
  OTLP to the Collector, which forwards them to Phoenix (and, optionally, to
  MLflow). The agent has no Phoenix SDK and no backend-specific code; nothing
  downstream depends on how the agent produced the spans, and adding or
  replacing a backend is a collector change. See [OBSERVABILITY.md](OBSERVABILITY.md).
* **Evaluation is repository-owned.** Datasets, deterministic scorers and
  quality gates live in `evaluation/` and call the agent through the same
  contract as the gateway; see [EVALUATION.md](EVALUATION.md). The evaluator
  writes the result itself; Phoenix and MLflow display a copy and never decide
  whether a run passed.

## What each boundary is for

| Boundary | Question it answers | Mechanism |
| --- | --- | --- |
| browser → UI | Is this a real, logged-in user, on our origin? | Keycloak OIDC session cookie, `SameSite`, CSRF double-submit |
| UI → gateway | — | Server-side call; the UI is a proxy, not a trust boundary |
| gateway → agent | Is this caller the gateway? | Static service credential, constant-time compared |
| gateway → agent | Who is the user? | Gateway-minted `x-authenticated-*` headers |
| model → tool call | May this proposed call run? | One deterministic policy function (`ToolPolicy::decide`) at Rig's dispatch hook: allow-listed tool, arguments valid against the published schema |
| agent → MCP | Is this caller the agent? | Static service credential |
| model → state | Did a human authorize this exact change? | Signed approval token (optional feature) |

The two service credentials are not redundant with network isolation. Network
membership answers *can this packet arrive*; it cannot answer *is this caller
the gateway*. The agent **trusts** the identity headers it receives — they end up in
audit records and in signed approval tokens — so it must authenticate its
callers.

## Network segmentation

Eight Compose networks, each one trust relationship:

| Network | Members | Purpose |
| --- | --- | --- |
| `edge` | ui, keycloak, phoenix, otel-collector, (mlflow), (mcp-inspector) | The only services that publish host ports |
| `gateway_net` | ui, gateway | assistant-ui → gateway |
| `auth_net` | gateway, keycloak, realm-init | OIDC backchannel |
| `agent_net` | gateway, agent, evaluator, (evaluator-mlflow) | → the agent |
| `mcp_net` | agent, mcp-server, (mcp-inspector) | → MCP |
| `data_net` | mcp-server, postgres | The database is reachable from one service |
| `telemetry_net` | agent, otel-collector | Trace export, and nothing else |
| `observability_net` | otel-collector, phoenix, evaluator, (mlflow), (evaluator-mlflow) | → the observability backends |

Parenthesised members exist only under a profile (`dev`, `mlflow`). Phoenix and
MLflow share no network with the agent, MCP, the gateway or PostgreSQL: both
have UIs that can make outbound requests on a user's behalf (LLM playgrounds),
so neither may become a browser-driven route to a protected service. Enabling
the `mlflow` profile changes no protected service's reachability;
`make compose-profile-test` asserts that.

The consequences are asserted twice: statically from the resolved configuration
(`scripts/verify_security_config.py`, run by `make security-config-test`) and at
runtime against the live cluster (`make network-test`).

None of these is `internal: true`. That flag removes a network's default route —
it blocks *egress* — and does nothing for inbound reachability, which `ports:`
already governs. The agent must reach the model endpoint, so marking its
networks internal would break inference while adding no protection this topology
does not already have.

## Where the model is, and is not, trusted

The model chooses **which read-only tools to call and what to say**. It does not
choose:

* **who the user is** — identity comes from gateway-minted headers, never from
  the conversation;
* **whether a change is authorized** — that requires a signed token the model
  cannot mint (see [APPROVALS.md](APPROVALS.md));
* **what a policy decides** — backend policy is re-evaluated at the point of
  mutation, after the human approves;
* **what reaches the client** — the output policy runs between the model and
  the stream.

The agent **runtime** is trusted, unlike the model. It holds the service
credential for the MCP server (`MCP_API_KEY`) and, when approvals are enabled,
the approval signing secret (`HITL_APPROVAL_SECRET`). Neither enters the model's
context or a tool argument. A compromise of the runtime is therefore a different
and more serious failure than a manipulated model; see
[APPROVALS.md — the trust model](APPROVALS.md#the-trust-model).

Text that arrives *through a tool result* is data, never instruction. That is a
property the evaluation suite measures rather than asserts: see the `injection`
suite in [EVALUATION.md](EVALUATION.md).

## Component map

| Path | What it is |
| --- | --- |
| `ui/` | assistant-ui on Next.js. Proxies to the gateway; renders Markdown without raw HTML. |
| `gateway/` | Rust BFF. OIDC, sessions, CSRF, proxying. Nine modules, see [SECURITY.md](SECURITY.md). |
| `agent/` | The agent: a Rig + Rust service — loop, tool policy, input and output policy, telemetry, optional approvals. See [AGENT-INTERNALS.md](AGENT-INTERNALS.md). |
| `mcp-server/` | Rust MCP tools over PostgreSQL, plus the approval verifier. |
| `evaluation/` | Backend-neutral evaluator: datasets, deterministic scorers, gates, provenance, result artifact; Phoenix and optional MLflow reporters. |
| `observability/` | OpenTelemetry Collector configuration: base (Phoenix) and the optional MLflow overlay. |
| `scripts/` | Verification that runs without the cluster, and trace tooling. |

## Building a domain application on this

See [EXTENDING.md](EXTENDING.md). In short: the support-tickets domain lives in
`db/init.sql`, the MCP tools, `agent/config.yml`'s prompt and tool list, and the
evaluation datasets. Everything else is infrastructure.
