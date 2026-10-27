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

The path is taught on this repository's agent: a Rust service built on the
[Rig](https://github.com/0xPlaygrounds/rig) framework. Each stage names the
source that realises its component and, where there is one, the matching
lesson in [AGENT-INTERNALS.md](AGENT-INTERNALS.md), the advanced appendix that
walks through the agent's code. You do not need to know Rust to follow the
path; you need it for the appendix.

One idea runs underneath every stage: **the agent framework is not the
architecture.** Rig runs the loop. Identity, authorization, tool access,
approvals, state transitions and every security boundary are deterministic
code that would survive replacing it — and once already did, when this agent
moved from NVIDIA NeMo Agent Toolkit to Rig without moving a single trust
boundary ([FRAMEWORK-MIGRATION.md](FRAMEWORK-MIGRATION.md)).

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
| 1 | Agent and agent loop | Agent runtime (Rig), loop and tool-call bounds | One model call cannot act on the world |
| 2 | Tool calling | Typed tools, native tool calling | Parsing prose into actions; unbounded actions |
| 3 | MCP and capability boundaries | Rust MCP server, `include:` list | An agent that can do whatever its credentials can |
| 4 | Grounding | Tools over the system of record | Confident answers from model memory |
| 5 | Guardrails and untrusted data | Input/output rails as explicit Rust policy, deterministic patterns | Hostile input; leaked secrets and PII |
| 6 | Evaluation | MLflow suites, deterministic scorers | "It worked when I tried it" |
| 7 | Observability | OpenTelemetry → MLflow traces | Not knowing what the agent actually did |
| 8 | Identity and trust boundaries | Keycloak, gateway, service credentials, segmented networks | The model or the browser choosing who the user is |
| 9 | Human-in-the-loop mutation | Signed approvals, point-of-mutation policy, audit | The model authorizing its own actions |
| 10 | Production architecture | All of the above, composed — independent of the framework | Mistaking the framework for the architecture |

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

**In this repo.** `LLM_BASE_URL` and `LLM_MODEL` select any OpenAI-compatible
endpoint ([CONFIGURATION.md](CONFIGURATION.md#model-endpoint)), and
`workflow.temperature` in [`agent/config.yml`](../agent/config.yml) is `0.0`. While writing these labs, the
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

**In this repo.** Rig's agent runner, built once per request in
[`builder.rs`](../agent/src/agent/builder.rs) and driven step by step in
[`execution.rs`](../agent/src/agent/execution.rs). The loop is bounded twice:
Rig's model-call budget, and a deterministic tool-call budget of exactly
`max_tool_calls` enforced in [`hooks.rs`](../agent/src/agent/hooks.rs). The
run itself moves through an explicit state machine
([`state.rs`](../agent/src/agent/state.rs)): `Received → ScreeningInput →
Running ⇄ AwaitingApproval → Completed / Failed / Cancelled`.
→ [Internals 03](AGENT-INTERNALS.md#03--understand-the-agent-loop),
[10](AGENT-INTERNALS.md#10--model-execution-as-an-explicit-state-machine)

The framework owns the loop and nothing else. Swapping it changes how the loop
is written, not who decides what the loop may do.

**Failure it addresses.** Models that can't act. Bounding the loop addresses its
own new failure: runaway loops.

**Lab.** [01 — Run the agent](tutorials/01-run-the-agent.md)
**Read.** [Concept 1](concepts/01-agents-and-agent-loops.md),
[Follow one request](tutorials/REQUEST-WALKTHROUGH.md)

## Stage 2: Tool calling

**Why we need it.** The loop needs a contract for "actions". **Tools** are typed
functions the model can *request*. **Native tool calling** returns those
requests as structured data rather than prose to be parsed.

**In this repo.** Native tool calling through Rig. `search_tickets` and
`get_ticket` with JSON Schemas derived from Rust structs in the MCP server.
Descriptions tuned in `tools.mcp.overrides` in `agent/config.yml`. Every
proposed call passes one deterministic decision point,
[`ToolPolicy::decide`](../agent/src/guardrails/tools.rs): closed registry,
closed argument schema, no type coercion.
→ [Internals 05](AGENT-INTERNALS.md#05--validate-model-proposed-tool-arguments),
[06](AGENT-INTERNALS.md#06--separate-model-proposals-from-software-authority)

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
`tools.mcp.include` list grants the tools. A granted tool that doesn't exist, or
whose schema the agent cannot enforce or that declares an identity field such as
`user_id`, stops the agent from starting
([`mcp/client.rs`](../agent/src/mcp/client.rs)).
→ [Internals 04](AGENT-INTERNALS.md#04--discover-tools-through-mcp)

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

**In this repo.** Input policy as pure functions in
[`guardrails/input.rs`](../agent/src/guardrails/input.rs): a length bound,
critical deny patterns, one LLM self-check whose verdict is parsed
deterministically, and anchored read-only allow templates, with fixed
precedence. If the classifier is unreachable the request is refused (fail
closed). Output policy in [`guardrails/output.rs`](../agent/src/guardrails/output.rs):
credential patterns blocked across chunk boundaries in a bounded streaming
window, and deterministic PII masking — also applied to the tool results shown
to people. → [GUARDRAILS.md](GUARDRAILS.md),
[Internals 07–08](AGENT-INTERNALS.md#07--stream-output-safely)

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

**In this repo.** One OpenTelemetry root span per request
([`api/workflow.rs`](../agent/src/api/workflow.rs)); the rails, Rig's own
`chat` and `execute_tool` spans, the tool policy, the MCP call and any approval
wait nest under it. Readable question and answer on the root, credentials and
raw identity never exported, opt-in pseudonymous user attribution, exported
over OTLP through the Collector to MLflow. OpenTelemetry is the boundary: the
backend is replaceable without touching the agent.
→ [OBSERVABILITY.md](OBSERVABILITY.md),
[Internals 13](AGENT-INTERNALS.md#13--trace-and-evaluate-the-whole-lifecycle)

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

In the agent, both checks run in its own middleware
([`api/auth.rs`](../agent/src/api/auth.rs)) and produce a
[`TrustedCaller`](../agent/src/identity.rs): a type with no public constructor,
never rendered into the prompt and never a tool argument.
→ [Internals 09](AGENT-INTERNALS.md#09--propagate-trusted-identity-outside-the-prompt)

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

**In this repo.** Optional and off by default. The model proposes
`ticket_priority_change`; [`approval/gate.rs`](../agent/src/approval/gate.rs)
suspends the run and asks the human; [`approval/pending.rs`](../agent/src/approval/pending.rs)
accepts an answer only from the prompted user and only with an offered choice,
and only then builds a `VerifiedDecision`; [`approval/token.rs`](../agent/src/approval/token.rs)
signs it. The MCP server verifies the token independently, then
[`mutation.rs`](../mcp-server/src/mutation.rs) applies nonce, row lock,
re-derived state, `apply_policy`, and the change plus an append-only audit row
in one transaction.
→ [Internals 11–12](AGENT-INTERNALS.md#11--suspend-for-human-approval)

**Failure it addresses.** The model authorizing its own actions; replayed,
stale or altered approvals; editable audit trails.

**Lab.** [08 — Add a state-changing action](tutorials/08-add-a-state-changing-action.md),
[09 — Add human approval](tutorials/09-add-human-approval.md)
**Read.** [Concept 8](concepts/08-human-in-the-loop.md), [APPROVALS.md](APPROVALS.md)

## Stage 10: Production agent architecture

**Why it looks like this.** Put the stages together and every component has a
specific job, with the model contained in the middle:

* the **gateway** decides who the user is;
* the **agent framework** (Rig) runs a bounded loop and decides nothing about authority;
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
| Agent framework | Rig 0.44, in a Rust agent service |
| Tool interoperability | MCP (streamable HTTP) |
| Capability implementation | Rust MCP server (`rmcp`, `sqlx`) |
| Guardrails | Explicit Rust policy modules + one LLM self-check |
| Tracing | OpenTelemetry → OpenTelemetry Collector |
| Experiment tracking, trace store | MLflow |
| Authentication | Keycloak (OIDC), Rust gateway (BFF) |
| Persistence | PostgreSQL |
| UI | assistant-ui on Next.js |

These are implementation choices. The concepts carry over to other runtimes,
protocols and stores — demonstrably: the agent framework has already been
replaced once, from NeMo Agent Toolkit to Rig, and the rest of this table did
not change ([FRAMEWORK-MIGRATION.md](FRAMEWORK-MIGRATION.md)). Four boundaries
are what made that possible, and they are the ones to keep in any design:

* **identity** is asserted by the gateway, outside the agent and the model;
* **MCP** is the capability boundary: the agent can do what the tools do, and
  nothing else;
* **OpenTelemetry** is the telemetry boundary: the trace backend is replaceable;
* **evaluation** datasets, scorers and gates belong to the repository, not to
  a framework or a vendor.

What doesn't carry over automatically is the discipline: deterministic
boundaries around a probabilistic core. Frameworks are replaceable; trust
boundaries are architecture.

**Before production,** read [LIMITATIONS.md](LIMITATIONS.md). It lists what this
template does *not* do (per-user data authorization, secrets management,
migrations, trace-store access control, a dedicated guard model, among others).

**Lab.** [10 — Build your own domain agent](tutorials/10-build-your-own-domain-agent.md)
**Then.** [CHALLENGES.md](CHALLENGES.md), [ANTI-PATTERNS.md](concepts/ANTI-PATTERNS.md)
