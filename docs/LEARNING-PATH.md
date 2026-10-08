# Learning path

A progressive curriculum for experienced software engineers who want to build
production AI agents. It assumes you know HTTP, APIs, databases, authentication,
Docker, distributed systems, testing and observability. It teaches what is
*different* when one component of your system is a probabilistic decision maker.

> **Agentic AI is software engineering around a probabilistic decision-making
> component.**
>
> **The LLM is an untrusted decision maker. Security and authorization must be
> enforced deterministically outside the model.**

Each stage answers one question: *why did we need the next architectural
component?* Each points to a concept page (the why), a lab (the hands-on), and the
reference manual (the precise how).

The path is taught on the canonical NeMo Agent Toolkit (NAT) implementation.
Each stage also says, in one line, how the alternative Rig + Rust
implementation on
[`rust-agent`](https://github.com/cognokratos/simple-agent-template/tree/rust-agent)
realises the same component. When you have finished the path, the optional
[Rust learning extension](RUST-LEARNING-PATH.md) follows the whole architecture
through that implementation, to expose what a high-level toolkit does for you.

## How long things take

| In | You can | Read |
| --- | --- | --- |
| 30 seconds | Say what this project is | [README](../README.md) |
| 5 minutes | Name the major components and why each exists | This page's [summary table](#the-path-at-a-glance), [ARCHITECTURE.md](ARCHITECTURE.md) |
| 30 minutes | Explain how one request becomes tool calls and an answer | [Follow one request](tutorials/REQUEST-WALKTHROUGH.md) |
| A few hours | Add a tool, run evaluations, read traces | Labs [01](tutorials/01-run-the-agent.md)–[07](tutorials/07-experiment-with-guardrails.md) |
| The full path | Replace the sample domain with your own production agent | Labs [08](tutorials/08-add-a-state-changing-action.md)–[10](tutorials/10-build-your-own-domain-agent.md), [CHALLENGES.md](CHALLENGES.md) |

## The path at a glance

| Stage | Concept | Component it adds | Failure it addresses |
| --- | --- | --- | --- |
| 0 | LLM as a probabilistic component | — | Treating model output as a return value |
| 1 | Agent and agent loop | Agent runtime (NAT ReAct; Rig), loop bounds | One model call cannot act on the world |
| 2 | Tool calling | Typed tools, native tool calling | Parsing prose into actions; unbounded actions |
| 3 | MCP and capability boundaries | Rust MCP server, `include:` list | An agent that can do whatever its credentials can |
| 4 | Grounding | Tools over the system of record | Confident answers from model memory |
| 5 | Guardrails and untrusted data | Input/output rails (NeMo Guardrails; explicit Rust policy), deterministic patterns | Hostile input; leaked secrets and PII |
| 6 | Evaluation | MLflow suites, deterministic scorers | "It worked when I tried it" |
| 7 | Observability | OpenTelemetry → MLflow traces | Not knowing what the agent actually did |
| 8 | Identity and trust boundaries | Keycloak, gateway, service credentials, segmented networks | The model or the browser choosing who the user is |
| 9 | Human-in-the-loop mutation | Signed approvals, point-of-mutation policy, audit | The model authorizing its own actions |
| 10 | Production architecture | All of the above, composed | — |

The whole path rests on one split:

> **Use the model for decisions that benefit from interpretation; use ordinary
> code for decisions that can be specified deterministically.**

| Probabilistic: the model | Deterministic: the code around it |
| --- | --- |
| interpreting intent | authentication |
| choosing tools where the request is ambiguous | authorization |
| synthesizing information from tool results | validation of inputs and tool arguments |
| handling natural-language ambiguity | business rules |
| recommendations and explanations | sorting and ranking when the rule is explicit |
| | state transitions |
| | approval verification |
| | persistence constraints |
| | audit records |
| | the allowed capability surface |
| | evaluation assertions |
| | network boundaries |

Lab 04 shows what happens when this line is drawn in the wrong place. Asked
"which ticket should we handle first?", the default model, in one set of
runs, picked a `high` ticket over an `urgent` one, working from grounded data. If "highest priority,
then oldest" is the rule, it can be written as an `ORDER BY`, and handing that
decision to the model adds risk without adding value. Let the model explain
the ranking; let code compute it.

---

## Stage 0: The LLM as a probabilistic component

**Concept.** A model call returns a *sample*: fluent, often right, sometimes
wrong, never guaranteed. It has no access to your systems and no memory between
calls.

**Why it matters.** Every later component exists because of this. You can't
unit-test the model into correctness, and you can't let it hold authority.

**In this repo.** `llms.primary` in [`agent/config.yml`](../agent/config.yml):
any OpenAI-compatible endpoint, `temperature: 0.0`. While writing these labs, the
same prompt, on the same model at temperature 0, with the same configuration
digest, behaved consistently within one agent build and differently after a
rebuild ([concept 3](concepts/03-grounding-and-authoritative-state.md#grounded-is-not-the-same-as-correct)).
Temperature 0 narrows variation. It does not make behaviour a stable property
of your configuration.

**Failure it addresses.** Treating model output as a return value you can trust.

**Lab.** [04 — Break the agent](tutorials/04-break-the-agent.md)
**Read.** [Concept 1](concepts/01-agents-and-agent-loops.md#the-llm-is-a-probabilistic-component),
[CONFIGURATION.md — model endpoint](CONFIGURATION.md#model-endpoint)

## Stage 1: Agent and agent loop

**Why we need it.** A single model call can only produce text. To answer *"what's
the history of TKT-1001?"* something has to fetch data, show it to the model and
let it continue. That something is an **agent runtime**: a loop that asks the
model for its next action, executes it, and feeds back the result.

**In this repo.** NAT's ReAct graph, wrapped by `streaming_react_agent` in
[`register.py`](https://github.com/cognokratos/simple-agent-template/blob/main/agent/src/nat_streaming_react/register.py). The loop is
bounded by `max_tool_calls`, `max_history`, retries and timeouts.

**On Rig.** Rig's agent runner, built per request in [`builder.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/agent/builder.rs) and driven in [`execution.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/agent/execution.rs); bounded by a model-call budget and a deterministic tool-call budget. → [Rust lesson 03](RUST-LEARNING-PATH.md#03--understand-the-agent-loop)

**Failure it addresses.** Models that can't act. Bounding the loop addresses its
own new failure: runaway loops.

**Lab.** [01 — Run the agent](tutorials/01-run-the-agent.md)
**Read.** [Concept 1](concepts/01-agents-and-agent-loops.md),
[Follow one request](tutorials/REQUEST-WALKTHROUGH.md)

## Stage 2: Tool calling

**Why we need it.** The loop needs a contract for "actions". **Tools** are typed
functions the model can *request*. **Native tool calling** returns those
requests as structured data rather than prose to be parsed.

**In this repo.** `use_native_tool_calling: true`. `search_tickets` and
`get_ticket` with JSON Schemas derived from Rust structs. Descriptions tuned in
`tool_overrides`.

**Failure it addresses.** Brittle text parsing, and actions with no contract.
It also introduces a new risk: tool descriptions are prompts, and tool
granularity determines how many round trips (and failure points) a task needs.

**Lab.** [02 — Understand tool calling](tutorials/02-understand-tool-calling.md)
**Read.** [Concept 2](concepts/02-tools-and-mcp.md)

## Stage 3: MCP and capability boundaries

**Why we need it.** If tools are functions inside the agent process, the agent
process needs every credential every tool needs. Moving tools behind a protocol
(MCP) into a separate service makes the **tool list the capability boundary**.
The agent can do exactly what the tools implement, and nothing else.

**In this repo.** The Rust MCP server ([`mcp-server/src/main.rs`](../mcp-server/src/main.rs)),
service-key authenticated, the only service on `data_net`. The agent's
`include:` list grants the tools. A granted tool that doesn't exist stops the
agent from starting.

**Failure it addresses.** An agent with database credentials, and a
`run_sql` tool. See [ANTI-PATTERNS.md](concepts/ANTI-PATTERNS.md#giving-the-model-direct-database-credentials).

**Lab.** [03 — Add an MCP tool](tutorials/03-add-an-mcp-tool.md)
**Read.** [Concept 2](concepts/02-tools-and-mcp.md#mcp-tools-as-a-protocol),
[ARCHITECTURE.md — network segmentation](ARCHITECTURE.md#network-segmentation)

## Stage 4: Grounding in authoritative systems

**Why we need it.** With tools available, the model can still answer from
memory. Grounding makes the **system of record** the only source of domain facts,
and makes answers checkable against what the tools returned.

**In this repo.** The system prompt's "use the tools for every factual
statement"; PostgreSQL `CHECK` constraints; the `grounding` evaluation suite.

**Failure it addresses.** Hallucinated authoritative state. It also shows the
limit: grounded inputs don't guarantee correct reasoning (lab 04, experiment 3).

**Lab.** [04 — Break the agent](tutorials/04-break-the-agent.md) (experiments 3–4)
**Read.** [Concept 3](concepts/03-grounding-and-authoritative-state.md)

## Stage 5: Guardrails and untrusted data

**Why we need it.** Users can send hostile input, and tool results can carry
hostile text. Answers can contain secrets or PII. Guardrails filter what goes in
and what comes out, using an LLM classifier backed by deterministic patterns.

**In this repo.** NeMo Guardrails via `text_guardrails`: length bound, critical
patterns, LLM self-check, anchored allow templates, regex output blocking,
buffered Presidio masking.

**On Rig.** The same input rules as pure functions plus one classifier call; secret blocking and deterministic PII masking in a bounded streaming window — narrower than Presidio. → [GUARDRAILS.md](GUARDRAILS.md#on-the-rig-implementation)

**Failure it addresses.** Direct prompt injection, harmful requests, credential
and PII disclosure. It does **not** address indirect injection or authorization.
Guardrails are not authorization.

**Lab.** [07 — Experiment with guardrails](tutorials/07-experiment-with-guardrails.md),
[04 — Break the agent](tutorials/04-break-the-agent.md) (experiment 1)
**Read.** [Concept 4](concepts/04-guardrails-and-deterministic-controls.md),
[GUARDRAILS.md](GUARDRAILS.md)

## Stage 6: Evaluation

**Why we need it.** Every previous stage changes model behaviour: prompts, tool
descriptions, rails. Without measurement, you can't tell whether a change helped
or hurt.

**In this repo.** Four suites (`tools`, `guardrails`, `grounding`, `injection`),
source-controlled datasets, deterministic scorers, gate metrics, latency
percentiles and provenance, all in MLflow.

**Failure it addresses.** Shipping on anecdotes. It also teaches its own lesson:
a scorer only catches what it asserts.

**Lab.** [05 — Evaluate the agent](tutorials/05-evaluate-the-agent.md)
**Read.** [Concept 5](concepts/05-evaluation.md), [EVALUATION.md](EVALUATION.md)

## Stage 7: Observability and traces

**Why we need it.** An evaluation tells you *that* a case failed. Only a trace
tells you *why*: which tools, which arguments, which rail decision, where the
time went.

**In this repo.** NAT and Guardrails spans joined into one trace per request
(`trace_context.py`), readable question and answer, header redaction, opt-in
user attribution, exported over OTLP to MLflow.

**On Rig.** One tracing model in one process: Rig's spans nest under the request's root span with no framework workaround. → [OBSERVABILITY.md](OBSERVABILITY.md#on-the-rig-implementation)

**Failure it addresses.** Debugging a runtime-chosen code path from scattered
logs, and accidentally turning the trace store into a credential or PII store.

**Lab.** [06 — Debug with traces](tutorials/06-debug-with-traces.md)
**Read.** [Concept 6](concepts/06-observability.md), [OBSERVABILITY.md](OBSERVABILITY.md)

## Stage 8: Authentication, identity and trust boundaries

**Why we need it.** Once the agent serves real users, it must know *who* is
asking, and neither the browser nor the model may decide that. Every internal
hop must prove *who is calling*, because services that trust identity headers
must only accept them from the component that minted them.

**In this repo.** Keycloak OIDC + PKCE in the Rust gateway; opaque sessions;
CSRF; schema re-serialisation; gateway-minted `x-authenticated-*` headers;
service credentials on gateway → agent and agent → MCP; seven segmented networks.

**On Rig.** The same checks in the service's own middleware, producing a `TrustedCaller` type the model cannot construct. → [SECURITY.md](SECURITY.md#on-the-rig-implementation)

**Failure it addresses.** Spoofed identity, model-chosen identity, and "it's on a
private network" as authentication.

**Lab.** [01 — Run the agent](tutorials/01-run-the-agent.md) (Break it)
**Read.** [Concept 7](concepts/07-security-and-trust-boundaries.md),
[SECURITY.md](SECURITY.md), [ARCHITECTURE.md](ARCHITECTURE.md)

## Stage 9: Human-in-the-loop and controlled mutations

**Why we need it.** Reading is recoverable. Writing is not. If the model can
change state, an injected instruction or a reasoning error becomes a real change
with an audit record that looks intentional. The model may propose. A human
decides. Deterministic code verifies and applies.

**In this repo.** Optional and off by default: `approval.py` (proposal → human
prompt → signed token), `interaction_guard.py` (only the prompted user, only an
offered choice), `mutation.rs` (nonce, row lock, re-derived state,
`apply_policy`, apply + append-only audit in one transaction).

**On Rig.** The run suspends on a `PendingTicket`; the interaction registry builds a `VerifiedDecision` only after owner and offer checks; same token, same MCP verifier. → [Rust lessons 11–12](RUST-LEARNING-PATH.md#11--suspend-for-human-approval)

**Failure it addresses.** The model authorizing its own actions; replayed,
stale or altered approvals; editable audit trails.

**Lab.** [08 — Add a state-changing action](tutorials/08-add-a-state-changing-action.md),
[09 — Add human approval](tutorials/09-add-human-approval.md)
**Read.** [Concept 8](concepts/08-human-in-the-loop.md), [APPROVALS.md](APPROVALS.md)

## Stage 10: Production agent architecture

**Why it looks like this.** Put the stages together and every component has a
specific job, with the model contained in the middle:

* the **gateway** decides who the user is;
* the **agent runtime** runs a bounded loop and decides nothing about authority;
* **guardrails** filter text at the edges of the loop;
* **MCP tools** define everything the model can cause;
* the **database** is the authority on state and enforces its own invariants;
* **approvals** turn model proposals into human-authorized, policy-checked,
  audited changes;
* **traces** record what happened, and **evaluations** measure how well.

See the full diagram, with trust boundaries, in
[ARCHITECTURE.md](ARCHITECTURE.md#overview), and the end-to-end request in
[Follow one request](tutorials/REQUEST-WALKTHROUGH.md).

| Concept | Implementation in this repo |
| --- | --- |
| Agent runtime | NeMo Agent Toolkit (NAT) 1.9 — canonical; Rig 0.44 in Rust on `rust-agent` |
| Tool interoperability | MCP (streamable HTTP) |
| Capability implementation | Rust MCP server (`rmcp`, `sqlx`) |
| Guardrails | NeMo Guardrails 0.21 + application-level deterministic layers (Rig: explicit Rust policy modules) |
| Tracing | OpenTelemetry → OpenTelemetry Collector |
| Experiment tracking, trace store | MLflow |
| Authentication | Keycloak (OIDC), Rust gateway (BFF) |
| Persistence | PostgreSQL |
| UI | assistant-ui on Next.js |

These are implementation choices. The concepts carry over to other runtimes,
protocols and stores. What doesn't carry over automatically is the discipline:
deterministic boundaries around a probabilistic core.

**Before production,** read [LIMITATIONS.md](LIMITATIONS.md). It lists what this
template does *not* do (per-user data authorization, secrets management,
migrations, trace-store access control, a dedicated guard model, among others).

**Lab.** [10 — Build your own domain agent](tutorials/10-build-your-own-domain-agent.md)
**Then.** [CHALLENGES.md](CHALLENGES.md), [ANTI-PATTERNS.md](concepts/ANTI-PATTERNS.md)
