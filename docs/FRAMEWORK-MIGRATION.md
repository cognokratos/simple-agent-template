# Framework migration: from NeMo Agent Toolkit to Rig

> **Frameworks are replaceable. Trust boundaries are architecture.**

This template's agent service was first built on NVIDIA NeMo Agent Toolkit
(NAT) 1.9 with NeMo Guardrails, in Python. It is now built on
[Rig](https://github.com/0xPlaygrounds/rig) 0.44 with explicit policy code, in
Rust. This page is the record of that migration, kept as evidence for the
central lesson of the curriculum: the agent framework is not the architecture.

| | Status | Where |
| --- | --- | --- |
| **Rig + Rust** | canonical, maintained | `main` — [`agent/`](../agent/) |
| **NAT + NeMo Guardrails + Python** | deprecated, frozen for comparison | tag [`nat-1.9-final`](https://github.com/cognokratos/simple-agent-template/tree/nat-1.9-final), branch `legacy/nat` |

The NAT implementation is not maintained and receives no fixes, including
security fixes. It is preserved so the comparison below can be reproduced, not
so it can be deployed:

```bash
git fetch origin --tags
git switch --detach nat-1.9-final   # the last NAT commit, never moved
make dev                            # its own README and docs describe it
```

## The lesson

Replacing the agent framework replaced the loop, the model client, the tool
dispatcher, the guardrail engine, the streaming path and the tracing
integration. It did **not** move a single trust boundary, and it did not change
a single byte that the gateway, the UI, the MCP server, the database or the
evaluation harness depends on. That is only possible because none of those
boundaries lived in the framework in the first place:

| Owned by the architecture, not the framework | Where it lives | Moved in the migration? |
| --- | --- | --- |
| Who the user is | the gateway (Keycloak OIDC), asserted to the agent with a service credential | no |
| What the model can cause | the MCP server's tool list and the agent's allow-list | no |
| Whether a mutation is permitted | `apply_policy` in the MCP server, after an approval token is verified | no |
| What the database accepts | PostgreSQL constraints and the append-only audit table | no |
| The agent-service contract | routes, SSE vocabulary, errors, `/version` ([AGENT-SERVICE-CONTRACT.md](AGENT-SERVICE-CONTRACT.md)) | no |
| Telemetry | OpenTelemetry over OTLP to the Collector, then MLflow | no |
| Evaluation | datasets, scorers and gates in [`evaluation/`](../evaluation/) | no |
| The model | any OpenAI-compatible endpoint | no |

The model is still the one probabilistic decision maker. Deterministic
software still owns identity, authorization, tool access, approvals, state
transitions and every security boundary. **Intelligence does not imply
authority** — and authority that never lived in the framework survives
replacing it.

## What was held constant

```text
same architecture        browser → assistant-ui → gateway → AGENT → MCP → PostgreSQL
same trust model         the model proposes; deterministic software decides
same application         support-ticket triage, same seed data
same MCP boundary        the same Rust MCP server, tools and approval verifier
same gateway             the same OIDC, sessions, CSRF and identity headers
same contract            AGENT-SERVICE-CONTRACT.md — routes, SSE, errors, provenance
same prompts             the same system and guardrail prompts
same evaluation          the same datasets, scorers and gates

different agent runtime  NAT + Python + NeMo Guardrails  →  Rig + Rust + explicit policy
```

The migration was developed as a single commit on a comparison branch whose
only differences from the NAT tree were `agent/` and the files that build and
check it. That commit was promoted to `main` (#6) and the comparison branch
retired; the diff is [`nat-1.9-final..main`](https://github.com/cognokratos/simple-agent-template/compare/nat-1.9-final...main),
read through `agent/`.

## What NAT provided

NAT is a capable toolkit, and much of what the Rust agent does it once did
through configuration:

* a ReAct agent graph with native tool calling, streaming and loop bounds;
* an MCP client function group: discovery, allow-list, description overrides,
  reconnect;
* NeMo Guardrails flows: an LLM self-check input rail and a regex output rail;
* Presidio NER masking of PII;
* an HTTP front end with an interaction (pause/resume) protocol;
* OpenTelemetry export, profiling and evaluation integrations, and a plugin
  ecosystem.

Several local modules on the NAT tree existed to bend that behaviour to this
architecture: an ASGI worker so identity refusals reached the client, an
interaction guard substituted into NAT's execution store so only the prompted
user could answer, and trace-context code relying on private NAT attributes to
get one trace per request. Their removal conditions are recorded at the tag
([`EXTENDING.md`](https://github.com/cognokratos/simple-agent-template/blob/nat-1.9-final/docs/EXTENDING.md#what-each-local-module-compensates-for)).

## How to read the comparison

Each concern below names how the retired NAT agent realised it, how the Rig
agent realises it, and how the two relate:

| Relation | Meaning |
| --- | --- |
| **equivalent** | the same property, enforced to the same extent |
| **stricter** | Rig enforces more than NAT did, or fails closed where NAT did not |
| **narrower** | Rig covers less than NAT did |
| **different** | a different mechanism with a different trade-off; neither dominates |

NAT source links point at the `nat-1.9-final` tag; Rust source links point at
this tree.

## Concern by concern

### The agent loop

| | |
| --- | --- |
| NAT (retired) | NAT's ReAct graph on LangGraph, wrapped by [`register.py`](https://github.com/cognokratos/simple-agent-template/blob/nat-1.9-final/agent/src/nat_streaming_react/register.py) to stream native tool-calling answers; bounded by a recursion limit derived from `max_tool_calls` |
| Rig (current) | Rig's `AgentRunner` over a serialisable `AgentRun` state machine, built per request in [`builder.rs`](../agent/src/agent/builder.rs) and driven in [`execution.rs`](../agent/src/agent/execution.rs); bounded by a model-call budget **and** a deterministic tool-call budget |
| Relation | **equivalent** loop; **stricter** bound (exactly `max_tool_calls` tool calls) |
| Takeaway | Both frameworks own the loop. Rig exposes it as a small state machine you can read; NAT hides it behind a configurable graph. The loop is never where authority is decided. |

### Model client

| | |
| --- | --- |
| NAT (retired) | LangChain `ChatOpenAI` through a NAT provider that omits empty optional parameters ([`llm_config.py`](https://github.com/cognokratos/simple-agent-template/blob/nat-1.9-final/agent/src/nat_streaming_react/llm_config.py)) |
| Rig (current) | Rig's OpenAI-compatible Chat Completions client ([`model.rs`](../agent/src/agent/model.rs)); an empty `reasoning_effort` is `None` in typed config |
| Relation | **equivalent** |
| Takeaway | "Empty means absent" needed a custom provider in NAT and is one `Option` in Rust: configuration semantics are part of the integration surface. |

### MCP and tool discovery

| | |
| --- | --- |
| NAT (retired) | NAT's `mcp_client` function group: streamable HTTP, `include:` allow-list, description overrides, reconnect |
| Rig (current) | `rmcp` 2.2 (the server's SDK line) through `rig-rmcp`'s `McpTool` ([`client.rs`](../agent/src/mcp/client.rs)); allow-list and overrides from config; tool schemas outside an enforceable subset, or declaring identity/approval fields, **stop startup** |
| Relation | **equivalent** boundary; **stricter** discovery |
| Takeaway | MCP stays the capability boundary in both. Neither agent touches the database. |

### Tool dispatch and schema validation

| | |
| --- | --- |
| NAT (retired) | Arguments parsed into pydantic models derived from the MCP schema; the tools that exist are whatever NAT was configured with |
| Rig (current) | Every model-proposed call passes Rig's `AgentHook::on_dispatch` ([`hooks.rs`](../agent/src/agent/hooks.rs)), which asks one pure function, [`ToolPolicy::decide`](../agent/src/guardrails/tools.rs): closed registry, JSON object only, no unknown fields, **no coercion** (`"5"` is not an integer), mutations only into the approval gate; the executors re-check the same policy |
| Relation | **stricter** |
| Takeaway | "The model proposes; deterministic software authorises" is a single, testable function on Rig, and is distributed across configuration and framework parsing on NAT. In both, the MCP server re-validates and stays authoritative. |

### Middleware and hooks

| | |
| --- | --- |
| NAT (retired) | NAT middleware (`text_guardrails`), a custom FastAPI worker, a substituted execution store, telemetry processors |
| Rig (current) | Rig `AgentHook` for tool policy; everything else is the service's own Axum middleware and plain functions |
| Relation | **different** |
| Takeaway | NAT's extension points let you change behaviour without owning the loop; Rig's let you own the loop without patching anything. The NAT tree needed several modules to work around specific toolkit behaviour ([EXTENDING.md at the tag](https://github.com/cognokratos/simple-agent-template/blob/nat-1.9-final/docs/EXTENDING.md#what-each-local-module-compensates-for)); the Rust agent needs none. |

### Trusted identity

| | |
| --- | --- |
| NAT (retired) | Service key, then exactly one `x-authenticated-user-id`, enforced by ASGI middleware in [`fastapi_worker.py`](https://github.com/cognokratos/simple-agent-template/blob/nat-1.9-final/agent/src/nat_streaming_react/fastapi_worker.py) because NAT's own refusal does not reach the client on the workflow routes |
| Rig (current) | The same two checks in [`api/auth.rs`](../agent/src/api/auth.rs); the result is a [`TrustedCaller`](../agent/src/identity.rs) with no public constructor |
| Relation | **equivalent**; the type makes "the model cannot forge identity" a compile-time property of the agent code |
| Takeaway | Identity comes from the gateway, never from conversation text, in both. |

### Streaming

| | |
| --- | --- |
| NAT (retired) | NAT generator through the guardrail middleware; SSE from NAT's front end |
| Rig (current) | Rig stream → output guard → bounded channel → Axum SSE body; dropping the body aborts the run |
| Relation | **equivalent** wire format ([contract](AGENT-SERVICE-CONTRACT.md)) |
| Takeaway | What is streamed is only what the output policy released, in both. |

### Input guardrails

| | |
| --- | --- |
| NAT (retired) | NeMo `self check input` + deterministic deny patterns and anchored allow templates ([`text_guardrails.py`](https://github.com/cognokratos/simple-agent-template/blob/nat-1.9-final/agent/src/nat_streaming_react/text_guardrails.py)) |
| Rig (current) | The same patterns, templates, precedence and prompt as pure functions ([`guardrails/input.rs`](../agent/src/guardrails/input.rs)); one classifier call parsed exactly as NeMo 0.21's `is_content_safe` |
| Relation | **equivalent**; Rig fails closed (refuses) when the classifier is unreachable |
| Takeaway | The only probabilistic part is the classifier's reply; how it is read and combined is deterministic on both. |

### Output guardrails and PII

| | |
| --- | --- |
| NAT (retired) | NeMo regex rail blocks credentials on a rolling window of chunks; **Presidio** masks PII, which forces the whole answer to be buffered first |
| Rig (current) | The same secret patterns in a bounded streaming window (320 characters held back, 512 of look-behind); **deterministic** PII recognisers (regex + Luhn, IBAN mod-97, Base58Check, Bech32) masked inside the same window ([`output.rs`](../agent/src/guardrails/output.rs), [`pii.rs`](../agent/src/guardrails/pii.rs)) |
| Relation | secrets **equivalent**; PII **narrower**; latency **different** |
| Takeaway | This is the clearest trade-off in the comparison, and it is not parity. Presidio combines patterns, context and a spaCy NER model; the Rust recognisers are stricter where an entity has a checksum and miss more where it does not (international phone layouts, anything found only through context). What the narrowing buys is a model-free output path that keeps streaming. |

### Tool results shown to people

| | |
| --- | --- |
| NAT (retired) | Tool steps reach the UI's tool card and the trace raw |
| Rig (current) | The display copy is redacted (secrets) and masked (PII); the model still reasons over the raw result |
| Relation | **stricter** |
| Takeaway | An output rail that covers only assistant text leaves the tool card as a side channel. |

### Execution state

| | |
| --- | --- |
| NAT (retired) | Implicit in NAT's runner |
| Rig (current) | An explicit enum with legal transitions ([`state.rs`](../agent/src/agent/state.rs)): `Received → ScreeningInput → Running ⇄ AwaitingApproval → Completed / Failed / Cancelled`, `ScreeningInput → Blocked` |
| Relation | **different** |
| Takeaway | Making invalid states unrepresentable — a blocked request cannot await approval, a finished run cannot resume — is cheap in Rust and worth copying into any runtime's design, even when the framework hides its own state. |

### Human-in-the-loop

| | |
| --- | --- |
| NAT (retired) | NAT's interaction layer paused the workflow coroutine; [`interaction_guard.py`](https://github.com/cognokratos/simple-agent-template/blob/nat-1.9-final/agent/src/nat_streaming_react/interaction_guard.py) adds owner and offered-choice checks; [`approval.py`](https://github.com/cognokratos/simple-agent-template/blob/nat-1.9-final/agent/src/nat_streaming_react/approval.py) prompts and mints |
| Rig (current) | [`approval/gate.rs`](../agent/src/approval/gate.rs) suspends the run on a `PendingTicket`; [`approval/pending.rs`](../agent/src/approval/pending.rs) checks the answer and only then builds a `VerifiedDecision` (private constructor); [`approval/token.rs`](../agent/src/approval/token.rs) mints the same token |
| Relation | **equivalent** security properties; **stricter** lifecycle (prompts expire; partial enablement refuses to start; the model never sees `actor_id`) |
| Takeaway | Same token, same MCP verifier, same single-use transaction. The difference is where the checks live: patched into a toolkit's store, or built into the type the run waits on. |

### Persistence of a suspended run

| | |
| --- | --- |
| NAT (retired) | In memory: a paused coroutine holding the client's stream |
| Rig (current) | In memory: an awaiting task holding the client's stream; Rig's `AgentRun` is serialisable, which is the piece a durable design would build on (not implemented) |
| Relation | **equivalent** (both lose pending approvals on restart) |
| Takeaway | Durable suspension needs more than a serialisable run: a reconnectable stream and a pending-record store. Part II of the curriculum covers it. |

### Tracing

| | |
| --- | --- |
| NAT (retired) | NAT's exporter and NeMo Guardrails' OTel spans, joined into one trace by pre-seeding NAT's root span through private NAT attributes |
| Rig (current) | `tracing` + `tracing-opentelemetry`; Rig adopts the request's root span, so its `chat` and `execute_tool` spans nest under it; the export filter is fixed in code because Rig logs whole provider requests at TRACE |
| Relation | **equivalent** goal (one trace per request); **different** mechanism |
| Takeaway | One tracing model in one process removes the hardest problem NAT's observability package solves. |

### Evaluation

| | |
| --- | --- |
| Both | The same harness, datasets, scorers and gates; the run is tagged `provenance.agent.runtime` (`nat` or `rig-rust`), and the current harness can still evaluate the tagged NAT agent |
| Relation | **equivalent** |
| Takeaway | The suites are the fair comparison. See [EVALUATION.md](EVALUATION.md#comparing-runtimes). |

### Deployment and runtime size

| | |
| --- | --- |
| NAT (retired) | Python slim image with NAT, NeMo Guardrails, Presidio and a spaCy model: **2.01 GB** (measured at `8ed2ceb`) |
| Rig (current) | Distroless image with one binary and its config: **71 MB**; no shell, so in-container checks use the binary's `probe` subcommand |
| Relation | **different** |
| Takeaway | Most of NAT's size is the capability it brings (NER, guardrail flows, integrations). Size alone is not the lesson. |

## Framework-provided behaviour vs explicit code

| Behaviour | NAT gave it to you | With Rig it is written here |
| --- | --- | --- |
| Agent loop, tool calling, streaming | yes | Rig provides it too |
| Guardrail flows, model-based checks, NER masking | yes (NeMo Guardrails, Presidio) | written by hand |
| Interaction/pause protocol | yes (with gaps closed by a local guard) | written by hand |
| Tool policy as one decision point | no — spread over configuration and parsing | written by hand, and therefore explicit |
| Execution state | hidden | written as an enum |
| Profiling, evaluation and plugin ecosystem | yes | no |

## Deliberate behavioural differences

Every difference between the two agents, in one list. None is claimed to be
equivalent.

| Area | NAT (`nat-1.9-final`) | Rig (current) | Relation |
| --- | --- | --- | --- |
| PII detection | Presidio, confidence floor 0.4 | regex + checksums | narrower |
| When masked output is released | whole answer buffered | progressively; last 320 characters held back | different |
| Tool results in UI and trace | raw | redacted for display | stricter |
| Approval result shown to the model | includes `actor_id` | `actor_id` removed | stricter |
| Tool-call budget | via recursion limit | exactly `max_tool_calls` | stricter |
| Tool argument coercion | pydantic coerces | refused | stricter |
| Tool schemas | trusted as published | refused at startup if unenforceable or identity-bearing | stricter |
| Pending approval | waits until disconnect | expires (`HITL_INTERACTION_TIMEOUT_SECONDS`) | stricter |
| Interactions without an owner | allowed unless strict | none exist | stricter |
| Unrecognised security boolean | default kept, warning | refuses to start | stricter |
| Partially enabled approvals | some combinations start read-only | refuses to start | stricter |
| Classifier unavailable | request errors | refused with a decision event | stricter |
| Model content on spans | NAT records LLM I/O | Rig content capture off by default | stricter |
| Route surface | NAT's full route set | contract routes only | stricter |
| Models verified | Ollama: `qwen3:8b`, `qwen3.5:9b` ([CONFIGURATION.md](CONFIGURATION.md#model-endpoint)) | Ollama: `qwen3:8b` | narrower |

Two of these were not free. PII detection is narrower: Presidio found entities
that pattern-and-checksum recognisers miss, and that is recorded in
[LIMITATIONS.md](LIMITATIONS.md). And the Rust agent has been exercised against
fewer models. Everything else moved in the stricter direction, and almost all
of it moved because the decision became ordinary code that could be made to
fail closed.

## What was measured

Single runs on a local stack (`qwen3:8b` on Ollama, both agents the same
model) — observations, not benchmarks:

| | Rig |
| --- | --- |
| "Show me the open support tickets" | `search_tickets{status: open}`, 15.6 s through the UI |
| "Which ticket should we handle first, and why?" | one `search_tickets` call, TKT-1002 chosen, 3.2 s |
| "Summarize ticket TKT-1003 and its history" | `get_ticket{TKT-1003}`, grounded, 8.4 s |
| Input-rail classifier call | 0.2–2.3 s |
| `make eval-all` | all four suites at their gates (1.0) |
| Agent image | 71 MB (NAT: 2.01 GB) |

[CONFIGURATION.md](CONFIGURATION.md#qwen38b-and-the-prioritization-prompt)
records that `qwen3:8b` struggled with the prioritisation prompt on NAT; on Rig
it answered from `search_tickets` alone in the runs observed. One run is not
evidence that the runtime caused it: the model and prompt are the same, but the
two clients build different request bodies (tool-schema rendering, message
structure).

## Why this demonstrates portability

The migration is a controlled experiment in which the framework is the only
variable. The evidence that the architecture is portable is not that the Rust
agent works; it is that **the checks around it did not change**:

* the gateway, MCP server, UI and database were not modified to accommodate it;
* `make security-test` asserts the same topology and authentication statuses;
* the MCP server verifies approval tokens the Rust agent mints with the same
  verifier it used for NAT's, and `cargo test --lib against_the_mcp_verifier`
  compiles that verifier into the agent's tests;
* the same evaluation datasets pass the same gates, and every run is tagged
  with the runtime that produced it
  ([EVALUATION.md](EVALUATION.md#comparing-runtimes)).

A framework is a dependency of the agent service. If replacing it would
require moving identity, authorization or capability checks, those checks were
in the wrong place.

## Choosing a framework, after the fact

* **NAT** suits breadth now: managed guardrail flows with model-based checks,
  Presidio's NER, a plugin ecosystem, profiling and evaluation integrations,
  and a Python team. You pay in dependency weight and in behaviour that lives
  in framework internals — the NAT tree needed compatibility shims and
  private-attribute access to get one trace.
* **Rig** suits the loop as a library inside code you own: a small dependency
  surface, compile-time-checked state, a single binary, and every security
  decision as ordinary code. You pay by writing what NAT gave you, and by
  tracking a younger framework whose API moved substantially between minor
  versions ([upgrading Rig](AGENT-INTERNALS.md#upgrading-rig)).

This template chose Rig because a teaching reference benefits most from
security decisions you can read as code. Either way, the boundary that matters
most does not move: the MCP server, not the agent, decides what may change in
the database.

## Further reading

* [AGENT-SERVICE-CONTRACT.md](AGENT-SERVICE-CONTRACT.md) — the boundary both implementations satisfied
* [AGENT-INTERNALS.md](AGENT-INTERNALS.md) — the Rust agent's internals, lesson by lesson
* [NAT-AGENT-ANALYSIS.md](NAT-AGENT-ANALYSIS.md) — the NAT agent's responsibilities, mapped before the Rust one was written
