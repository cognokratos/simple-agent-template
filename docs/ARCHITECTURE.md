# Architecture and trust boundaries

A template for a secured, observable, evaluated LLM agent. The sample
application triages customer-support tickets; everything that is not the
sample is meant to be reused unchanged.

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
NAT (NeMo Agent Toolkit)          publishes nothing
  │  service credential
  ▼
Rust MCP server                   publishes nothing
  │
  ▼
PostgreSQL                        publishes nothing
```

Alongside it:

```
NAT  ──OTLP──▶  OpenTelemetry Collector  ──▶  MLflow
evaluator ──▶  NAT (service credential, bypassing the browser path)
```

## What each boundary is for

| Boundary | Question it answers | Mechanism |
| --- | --- | --- |
| browser → UI | Is this a real, logged-in user, on our origin? | Keycloak OIDC session cookie, `SameSite`, CSRF double-submit |
| UI → gateway | — | Server-side call; the UI is a proxy, not a trust boundary |
| gateway → NAT | Is this caller the gateway? | Static service credential, constant-time compared |
| gateway → NAT | Who is the user? | Gateway-minted `x-authenticated-*` headers |
| NAT → MCP | Is this caller the agent? | Static service credential |
| model → state | Did a human authorize this exact change? | Signed approval token (optional feature) |

The two service credentials are not redundant with network isolation. Network
membership answers *can this packet arrive*; it cannot answer *is this caller
the gateway*. NAT **trusts** the identity headers it receives — they end up in
audit records and in signed approval tokens — so it must authenticate its
callers.

## Network segmentation

Seven Compose networks, each one trust relationship:

| Network | Members | Purpose |
| --- | --- | --- |
| `edge` | ui, keycloak, mlflow, otel-collector, (mcp-inspector) | The only services that publish host ports |
| `gateway_net` | ui, gateway | assistant-ui → gateway |
| `auth_net` | gateway, keycloak, realm-init | OIDC backchannel |
| `agent_net` | gateway, agent, evaluator | → NAT |
| `mcp_net` | agent, mcp-server, (mcp-inspector) | → MCP |
| `data_net` | mcp-server, postgres | The database is reachable from one service |
| `telemetry_net` | agent, otel-collector, mlflow, evaluator | Trace export |

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
* **what reaches the client** — output rails run between the model and the
  stream.

Text that arrives *through a tool result* is data, never instruction. That is a
property the evaluation suite measures rather than asserts: see the `injection`
suite in [EVALUATION.md](EVALUATION.md).

## Component map

| Path | What it is |
| --- | --- |
| `ui/` | assistant-ui on Next.js. Proxies to the gateway; renders Markdown without raw HTML. |
| `gateway/` | Rust BFF. OIDC, sessions, CSRF, proxying. Nine modules, see [SECURITY.md](SECURITY.md). |
| `agent/` | NAT workflow, guardrail middleware, observability, optional approvals. |
| `mcp-server/` | Rust MCP tools over PostgreSQL, plus the approval verifier. |
| `evaluation/` | MLflow suites, deterministic scorers, provenance. |
| `observability/` | OpenTelemetry Collector configuration. |
| `scripts/` | Verification that runs without the cluster, and trace tooling. |

## Building a domain application on this

See [EXTENDING.md](EXTENDING.md). In short: the support-tickets domain lives in
`db/init.sql`, the MCP tools, `agent/config.yml`'s prompt and tool list, and the
evaluation datasets. Everything else is infrastructure.
