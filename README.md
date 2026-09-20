# Secured agent template

A working, secured, observable, evaluated LLM agent you can fork and point at
your own domain.

```text
Browser / assistant-ui
    ↓ Keycloak login; opaque HttpOnly BFF session; CSRF
Rust authentication gateway
    ↓ static service credential; gateway-minted identity headers
NeMo Agent Toolkit ReAct workflow      ← NeMo Guardrails input/output rails
    ↓ static service credential
Rust MCP server
    ↓ parameterized SQLx queries
PostgreSQL

NAT + Guardrails spans ──OTLP──▶ OpenTelemetry Collector ──▶ MLflow
```

assistant-ui is the only application service reachable from the browser. The
gateway, NAT, MCP and the database publish no host ports and sit on segmented
networks. Any OpenAI-compatible model endpoint works; the defaults target a
local Ollama.

The sample application triages customer-support tickets for a fictional online
shop and is **read-only** by default. Everything that is not the sample is
meant to be reused unchanged — see [docs/EXTENDING.md](docs/EXTENDING.md).

## Start

```bash
make env          # create .env from .env.example
make pull-models  # no-op unless LLM_BASE_URL is an Ollama endpoint
make dev          # build and start everything
make wait         # readiness
make open-ui      # http://localhost:3000
```

Sign in with `agent` / `agent`. Then try:

```
Show me the open support tickets
Which ticket should we handle first, and why?
Summarize ticket TKT-1003 and its history
```

The prioritization prompt needs the agent to reason over several tickets at
once, which is more than the shipped default model, `qwen3:8b`, reliably
manages — see [docs/CONFIGURATION.md#model-endpoint](docs/CONFIGURATION.md#model-endpoint)
for what was observed and a tested alternative. The other two prompts are
single-tool and answer reliably on the default.

Changing a ticket's priority ("Mark it as high priority") is disabled by
default: the sample application ships read-only. To try the human-approval
flow, opt in per [docs/APPROVALS.md](docs/APPROVALS.md) — in short, uncomment
the `functions:` block in `agent/config.yml`, add `ticket_priority_change` to
`workflow.tool_names`, and set `HITL_APPROVAL_SECRET` and
`HITL_ENABLE_INTERACTIVE=true`. With that enabled, asking to change a ticket's
priority shows an approval card requiring a human decision and, for any actual
change, a typed reason; the choice is bound to a signed token, verified and
applied by the MCP server in one transaction, and recorded in `ticket_audit`.

## What this template gives you

| | |
| --- | --- |
| **Authentication** | Keycloak OIDC with PKCE, server-side tokens, opaque sessions, CSRF, strict cookie attributes |
| **Isolation** | Seven Compose networks, one per trust relationship, asserted statically *and* at runtime |
| **Guardrails** | NeMo input self-check with deterministic override layers; streaming secret blocking; configurable PII masking |
| **Observability** | One trace per request covering the agent run *and* the guardrail decisions, with readable question/answer and credential redaction |
| **Evaluation** | Four MLflow suites with deterministic scorers, latency distributions, and provenance linking every result to the agent that produced it |
| **Approvals** | An optional, opt-in signed-approval boundary for state-changing actions — off by default |

## Documentation

| Document | For |
| --- | --- |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | The request path, trust boundaries, network segmentation, where the model is and is not trusted |
| [SECURITY.md](docs/SECURITY.md) | Each control, why it exists, and how to check it |
| [CONFIGURATION.md](docs/CONFIGURATION.md) | Every setting |
| [GUARDRAILS.md](docs/GUARDRAILS.md) | Input and output rails, and what the pinned Guardrails release actually does |
| [OBSERVABILITY.md](docs/OBSERVABILITY.md) | The trace pipeline, content capture policy, and what redaction does not cover |
| [EVALUATION.md](docs/EVALUATION.md) | The suites, the scoring methodology, and provenance |
| [APPROVALS.md](docs/APPROVALS.md) | The optional human-approval boundary |
| [VERIFICATION.md](docs/VERIFICATION.md) | What you can check, what it needs, what it proves |
| [EXTENDING.md](docs/EXTENDING.md) | Building a domain application on this |
| [LIMITATIONS.md](docs/LIMITATIONS.md) | Known gaps, untested behaviour, and production prerequisites |
| [TEST-SCENARIOS.md](docs/TEST-SCENARIOS.md) | Prompts to type, and what should happen |

## Verify it

```bash
make static-check   # no Docker, no cluster, no model
make test           # everything, with the cluster up
make security-test  # authentication and topology boundaries
make eval-all       # the evaluation suites; needs a model
```

`make help` lists every target.

## Repository layout

| Path | |
| --- | --- |
| `ui/` | assistant-ui on Next.js |
| `gateway/` | Rust backend-for-frontend: OIDC, sessions, CSRF, proxying |
| `agent/` | NAT workflow, guardrail middleware, observability, optional approvals |
| `mcp-server/` | Rust MCP tools over PostgreSQL, and the approval verifier |
| `evaluation/` | MLflow suites, deterministic scorers, provenance |
| `db/`, `keycloak/`, `observability/` | Schema and seed data, realm generation, collector config |
| `scripts/` | Checks that run without the cluster, and trace tooling |

## Notes

**NAT ReAct prompt compatibility.** The custom `system_prompt` must contain the
`{tools}` and `{tool_names}` placeholders. NAT replaces them at startup with the
discovered MCP tool descriptions and names.

**No site-packages are modified.** Earlier revisions patched installed NAT and
Guardrails code at image-build time. That is now application code reached
through supported extension points, with regression suites proving the behaviour
it replaced. Where private NAT attributes are still relied on, they are named
with their removal conditions in
[OBSERVABILITY.md](docs/OBSERVABILITY.md).

**Dependency trade-off.** NAT 1.8's supported `react_agent` lives in the NAT
LangChain plugin and exposes no OpenAI-only extra, so this installs the full
LangChain dependency set. The expensive layer is cached, and the compiler needed
by `annoy` stays in the builder stage.

**Licensing.** Source files under `agent/src/` and `gateway/Cargo.toml` declare
Apache-2.0. There is no root `LICENSE` file; see
[LIMITATIONS.md](docs/LIMITATIONS.md#licensing).
