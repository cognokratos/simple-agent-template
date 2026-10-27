# Production AI Agent Template

A hands-on reference architecture for software engineers learning how to build
**secure, observable and evaluated AI agents**. It is also a real template you
can fork and point at your own domain.

> Agentic AI is software engineering around a probabilistic decision-making
> component. The LLM is an untrusted decision maker: security and authorization
> are enforced deterministically, outside the model.

## Where this fits in CognoKratos

This repository is **Part I — Production Agent Engineering** in the current
[CognoKratos curriculum](https://github.com/cognokratos/.github/blob/main/CURRICULUM.md):
an open-source, community-built curriculum for engineers learning how to build
autonomous systems that can exercise real capabilities without surrendering
security, verifiability or human control.

This is the foundation lab. It teaches how to treat the model as one
probabilistic component inside a larger trust architecture: identity, tool
capabilities, deterministic controls, evaluation, observability and controlled
mutation all live outside the model.

> **Core lesson:** Intelligence does not imply authority.

The repository is a laboratory, not a claim that there is one correct
architecture. Read the [CognoKratos foundation](https://github.com/cognokratos/.github/blob/main/FOUNDATION.md),
follow the structured synthesis in the [CognoKratos Book](https://book.cognokratos.com/part-1/introduction.html),
or help [challenge and extend the curriculum](https://github.com/cognokratos/.github/blob/main/CONTRIBUTING.md).

| I want to… | Go to |
| --- | --- |
| Run it | [Quick start](#quick-start) |
| Learn agentic AI engineering | [Learning path](docs/LEARNING-PATH.md) |
| See one request end to end | [Follow one request](docs/tutorials/REQUEST-WALKTHROUGH.md) |
| Understand the architecture | [Architecture](docs/ARCHITECTURE.md) |
| Build my own agent | [Extending the template](docs/EXTENDING.md) · [Labs](docs/tutorials/README.md) |
| Study the security model | [Security](docs/SECURITY.md) · [Trust boundaries](docs/concepts/07-security-and-trust-boundaries.md) |
| Learn evaluation | [Evaluation](docs/EVALUATION.md) · [Concept](docs/concepts/05-evaluation.md) |
| Debug agent execution | [Observability](docs/OBSERVABILITY.md) · [Lab: traces](docs/tutorials/06-debug-with-traces.md) |
| See why the framework is not the architecture | [Framework migration](docs/FRAMEWORK-MIGRATION.md) |

![simple-agent-template production AI agent architecture](docs/assets/simple-agent-template-architecture.svg)

The architecture is deliberately asymmetric: deterministic software controls identity,
authorization, tool access and state, while the LLM remains the single probabilistic
decision-making component inside those boundaries.

The agent service is written in Rust on the [Rig](https://github.com/0xPlaygrounds/rig)
agent framework. Rig runs the loop; it does not own the architecture. Identity,
tool access, approvals and state transitions are ordinary, tested code around
it, MCP is the capability boundary, OpenTelemetry is the telemetry boundary,
and the evaluation datasets and gates belong to this repository.

assistant-ui is the only application service reachable from the browser. The
gateway, the agent, the MCP server and the database publish no host
ports and sit on segmented networks. Any OpenAI-compatible model endpoint works; the defaults target a
local Ollama.

The sample application triages customer-support tickets for a fictional online
shop and is **read-only** by default. Everything that is not the sample is
meant to be reused unchanged — see [docs/EXTENDING.md](docs/EXTENDING.md).

## Three ways to use this repository

| Path | Question | Start with |
| --- | --- | --- |
| **LEARN** | "Teach me how production AI agents work." | [Learning path](docs/LEARNING-PATH.md) → [concepts](docs/README.md#learn-teach-me-how-production-ai-agents-work) → [labs](docs/tutorials/README.md) |
| **BUILD** | "Help me adapt this template to my domain." | [EXTENDING.md](docs/EXTENDING.md) → [Lab 10](docs/tutorials/10-build-your-own-domain-agent.md) → [challenges](docs/CHALLENGES.md) |
| **REFERENCE** | "Tell me precisely how this implementation works." | [Reference manual](docs/README.md#reference-tell-me-precisely-how-this-implementation-works) |

## Quick start

**Resources.** The full stack runs several services at once: assistant-ui,
the gateway, Keycloak, PostgreSQL, the MCP server, the agent, the
OpenTelemetry Collector and MLflow. Plan for a Docker VM of about 8 GB; see
[LIMITATIONS.md — resource requirements](docs/LIMITATIONS.md#resource-requirements).
To run the agent's own tests without Docker you need a Rust toolchain
(≥ 1.95).

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
default: the sample application ships read-only. With the human-approval flow
enabled, asking to change a ticket's priority shows an approval card requiring a
human decision and, for any actual change, a typed reason; the choice is bound
to a signed token, verified and applied by the MCP server in one transaction,
and recorded in `ticket_audit`. To enable it, set `HITL_APPROVAL_SECRET` (at
least 24 characters) and `HITL_ENABLE_INTERACTIVE=true`, and uncomment the
`tools.approval` block in `agent/config.yml`. The agent refuses to start with
only some of the switches set.

Then `make up-build` (it rebuilds the agent image, which bakes in `config.yml`, and
recreates the services whose settings changed). Details: [docs/APPROVALS.md](docs/APPROVALS.md#enabling-it).

## What this template gives you

| | |
| --- | --- |
| **Authentication** | Keycloak OIDC with PKCE, server-side tokens, opaque sessions, CSRF, strict cookie attributes |
| **Isolation** | Seven Compose networks, one per trust relationship, asserted statically *and* at runtime |
| **Agent** | Rig agent loop with streaming, an explicit execution state machine, a deterministic `ToolPolicy` over every model-proposed call, and an exact tool-call budget |
| **Guardrails** | An LLM input self-check that fails closed, with deterministic override layers; streaming secret blocking; deterministic PII masking, also applied to the tool results people see |
| **Observability** | One trace per request covering the agent run *and* the guardrail decisions, with readable question/answer and credential redaction |
| **Evaluation** | Four MLflow suites with deterministic scorers, latency distributions, and provenance linking every result to the agent that produced it |
| **Approvals** | An optional, opt-in signed-approval boundary for state-changing actions — off by default |
| **Learning layer** | A [learning path](docs/LEARNING-PATH.md), eight concept pages, an [end-to-end request walkthrough](docs/tutorials/REQUEST-WALKTHROUGH.md), ten [labs](docs/tutorials/README.md) and [challenges](docs/CHALLENGES.md), all grounded in this code and checked by `make docs-check` |

## Frameworks are replaceable; trust boundaries are architecture

This agent was first built on NVIDIA NeMo Agent Toolkit (NAT) in Python, and
later rebuilt on Rig in Rust. The rebuild replaced the loop, the model client,
the tool dispatcher, the guardrail engine and the tracing integration — and
moved no trust boundary. The gateway, the MCP server, the database, the UI, the
agent-service contract and the evaluation suites were not changed to
accommodate it.

That is the lesson the migration demonstrates, and
[FRAMEWORK-MIGRATION.md](docs/FRAMEWORK-MIGRATION.md) records the evidence,
concern by concern. The NAT implementation is deprecated and frozen for
comparison at the tag
[`nat-1.9-final`](https://github.com/cognokratos/simple-agent-template/tree/nat-1.9-final)
(also the branch `legacy/nat`); it is not maintained.

## Documentation

The full map, organised by the three paths above, is in
[docs/README.md](docs/README.md). The learning material links into the
reference documents rather than duplicating them, and
[AGENT-INTERNALS.md](docs/AGENT-INTERNALS.md) opens the Rust agent itself,
lesson by lesson.

## Contribute to the curriculum

CognoKratos contributions are not limited to feature work. A useful contribution
can be a reproducible failure, a stronger threat model, an adversarial test, an
alternative architecture, a new lab or a better explanation of a trade-off.
Reference architectures are propositions to inspect and challenge.

See the organization-level [contribution model](https://github.com/cognokratos/.github/blob/main/CONTRIBUTING.md)
for how project improvements can feed back into the living curriculum.

## Verify it

```bash
make static-check   # no Docker, no cluster, no model
make agent-check    # the agent's fmt, clippy and test suites (host Rust toolchain; no cluster, no model)
make test           # everything, with the cluster up
make security-test  # authentication and topology boundaries
make eval-all       # the evaluation suites; needs a model
make docs-check     # documentation links, anchors and make targets (part of static-check)
```

`make help` lists every target.

## Repository layout

| Path | |
| --- | --- |
| `ui/` | assistant-ui on Next.js |
| `gateway/` | Rust backend-for-frontend: OIDC, sessions, CSRF, proxying |
| `agent/` | The Rig + Rust agent: loop, tool policy, guardrails, identity, approvals, telemetry ([internals](docs/AGENT-INTERNALS.md)) |
| `mcp-server/` | Rust MCP tools over PostgreSQL, and the approval verifier |
| `evaluation/` | MLflow suites, deterministic scorers, provenance |
| `db/`, `keycloak/`, `observability/` | Schema and seed data, realm generation, collector config |
| `scripts/` | Checks that run without the cluster, and trace tooling |
| `docs/` | [Learning path](docs/LEARNING-PATH.md), [concepts](docs/concepts/), [labs](docs/tutorials/), and the reference manual |

## Notes

**Prompt placeholders.** The system prompt must contain the `{tools}` and
`{tool_names}` placeholders. The agent replaces them at startup with the
discovered MCP tool descriptions and names.

**Renamed variables.** The agent's service-key and trace-content variables lost
their historical `NAT_` prefix (`AGENT_API_KEY`, `AGENT_TRACE_CAPTURE_CONTENT`,
`AGENT_TRACE_CONTENT_MAX_CHARS`). The old names are still read, with a warning,
when the new ones are unset; see
[CONFIGURATION.md — renamed variables](docs/CONFIGURATION.md#renamed-variables).

**Licensing.** Original code and documentation are MIT licensed
([LICENSE](LICENSE)). The Rust agent contains no code derived from NVIDIA NeMo
Agent Toolkit; the Apache-2.0 files that were, and their NVIDIA notices, remain
at the `nat-1.9-final` tag. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
and [LIMITATIONS.md](docs/LIMITATIONS.md#licensing).
