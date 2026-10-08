# NAT vs Rig: one architecture, two agent runtimes

This template has two implementations of the same agent service:

| | Branch | Agent runtime | Role |
| --- | --- | --- | --- |
| **Canonical** | [`main`](https://github.com/cognokratos/simple-agent-template/tree/main) | NVIDIA NeMo Agent Toolkit (NAT) + NeMo Guardrails, Python | the recommended production reference |
| **Alternative** | [`rust-agent`](https://github.com/cognokratos/simple-agent-template/tree/rust-agent) | [Rig](https://github.com/0xPlaygrounds/rig) 0.44 + explicit policy code, Rust | a comparative learning implementation |

The Rig implementation exists for one reason: to implement the *same*
production-agent architecture with a thinner framework, so that what an agent
runtime actually does — the loop, tool dispatch, policy, streaming, MCP,
suspension for a human — is visible as ordinary code instead of toolkit
configuration. It is not a replacement for NAT, and this page does not pick a
winner. The question it answers is:

> What changes when the same architecture is implemented using a high-level
> agent toolkit versus a thinner Rust framework?

## What is held constant

```text
same architecture        browser → assistant-ui → gateway → AGENT → MCP → PostgreSQL
same trust model         the model proposes; deterministic software decides
same application         support-ticket triage, same seed data
same MCP boundary        the same Rust MCP server, tools and approval verifier
same gateway             the same OIDC, sessions, CSRF and identity headers
same contract            AGENT-SERVICE-CONTRACT.md — routes, SSE, errors, provenance
same prompts             byte-identical system and guardrail prompts (equal prompt_sha256)
same evaluation          the same datasets, scorers and gates

different agent runtime  NAT + Python + NeMo Guardrails  vs  Rig + Rust + explicit policy
```

Everything outside `agent/` is identical on the two branches, apart from the
build files that run the agent (Compose service, Makefile targets, CI job) and
the checks that inspect agent source. The documentation is identical too: it
lives on `main` and describes both.

## How to read the comparison

Each concern below names the NAT realisation, the Rig realisation, and how the
two relate:

| Relation | Meaning |
| --- | --- |
| **equivalent** | the same property, enforced to the same extent |
| **stricter** | Rig enforces more than NAT, or fails closed where NAT does not |
| **narrower** | Rig covers less than NAT |
| **different** | a different mechanism with a different trade-off; neither dominates |

Source links point at each branch explicitly: NAT files exist only on `main`,
Rust files only on `rust-agent`.

## Concern by concern

### The agent loop

| | |
| --- | --- |
| NAT | NAT's ReAct graph on LangGraph, wrapped by [`register.py`](https://github.com/cognokratos/simple-agent-template/blob/main/agent/src/nat_streaming_react/register.py) to stream native tool-calling answers; bounded by a recursion limit derived from `max_tool_calls` |
| Rig | Rig's `AgentRunner` over a serialisable `AgentRun` state machine, built per request in [`builder.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/agent/builder.rs) and driven in [`execution.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/agent/execution.rs); bounded by a model-call budget **and** a deterministic tool-call budget |
| Relation | **equivalent** loop; **stricter** bound (exactly `max_tool_calls` tool calls) |
| Takeaway | Both frameworks own the loop. Rig exposes it as a small state machine you can read; NAT hides it behind a configurable graph. The loop is never where authority is decided. |

### Model client

| | |
| --- | --- |
| NAT | LangChain `ChatOpenAI` through a NAT provider that omits empty optional parameters ([`llm_config.py`](https://github.com/cognokratos/simple-agent-template/blob/main/agent/src/nat_streaming_react/llm_config.py)) |
| Rig | Rig's OpenAI-compatible Chat Completions client ([`model.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/agent/model.rs)); an empty `reasoning_effort` is `None` in typed config |
| Relation | **equivalent** |
| Takeaway | "Empty means absent" needed a custom provider in NAT and is one `Option` in Rust: configuration semantics are part of the integration surface. |

### MCP and tool discovery

| | |
| --- | --- |
| NAT | NAT's `mcp_client` function group: streamable HTTP, `include:` allow-list, description overrides, reconnect |
| Rig | `rmcp` 2.2 (the server's SDK line) through `rig-rmcp`'s `McpTool` ([`client.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/mcp/client.rs)); allow-list and overrides from config; tool schemas outside an enforceable subset, or declaring identity/approval fields, **stop startup** |
| Relation | **equivalent** boundary; **stricter** discovery |
| Takeaway | MCP stays the capability boundary in both. Neither agent touches the database. |

### Tool dispatch and schema validation

| | |
| --- | --- |
| NAT | Arguments parsed into pydantic models derived from the MCP schema; the tools that exist are whatever NAT was configured with |
| Rig | Every model-proposed call passes Rig's `AgentHook::on_dispatch` ([`hooks.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/agent/hooks.rs)), which asks one pure function, [`ToolPolicy::decide`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/guardrails/tools.rs): closed registry, JSON object only, no unknown fields, **no coercion** (`"5"` is not an integer), mutations only into the approval gate; the executors re-check the same policy |
| Relation | **stricter** |
| Takeaway | "The model proposes; deterministic software authorises" is a single, testable function on Rig, and is distributed across configuration and framework parsing on NAT. In both, the MCP server re-validates and stays authoritative. |

### Middleware and hooks

| | |
| --- | --- |
| NAT | NAT middleware (`text_guardrails`), a custom FastAPI worker, a substituted execution store, telemetry processors |
| Rig | Rig `AgentHook` for tool policy; everything else is the service's own Axum middleware and plain functions |
| Relation | **different** |
| Takeaway | NAT's extension points let you change behaviour without owning the loop; Rig's let you own the loop without patching anything. On `main` several modules exist to work around specific toolkit behaviour (see [EXTENDING.md](EXTENDING.md#what-each-local-module-compensates-for)); `rust-agent` has none. |

### Trusted identity

| | |
| --- | --- |
| NAT | Service key, then exactly one `x-authenticated-user-id`, enforced by ASGI middleware in [`fastapi_worker.py`](https://github.com/cognokratos/simple-agent-template/blob/main/agent/src/nat_streaming_react/fastapi_worker.py) because NAT's own refusal does not reach the client on the workflow routes |
| Rig | The same two checks in [`api/auth.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/api/auth.rs); the result is a [`TrustedCaller`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/identity.rs) with no public constructor |
| Relation | **equivalent**; the type makes "the model cannot forge identity" a compile-time property of the agent code |
| Takeaway | Identity comes from the gateway, never from conversation text, in both. |

### Streaming

| | |
| --- | --- |
| NAT | NAT generator through the guardrail middleware; SSE from NAT's front end |
| Rig | Rig stream → output guard → bounded channel → Axum SSE body; dropping the body aborts the run |
| Relation | **equivalent** wire format ([contract](AGENT-SERVICE-CONTRACT.md)) |
| Takeaway | What is streamed is only what the output policy released, in both. |

### Input guardrails

| | |
| --- | --- |
| NAT | NeMo `self check input` + deterministic deny patterns and anchored allow templates ([`text_guardrails.py`](https://github.com/cognokratos/simple-agent-template/blob/main/agent/src/nat_streaming_react/text_guardrails.py)) |
| Rig | The same patterns, templates, precedence and prompt as pure functions ([`guardrails/input.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/guardrails/input.rs)); one classifier call parsed exactly as NeMo 0.21's `is_content_safe` |
| Relation | **equivalent**; Rig fails closed (refuses) when the classifier is unreachable |
| Takeaway | The only probabilistic part is the classifier's reply; how it is read and combined is deterministic on both. |

### Output guardrails and PII

| | |
| --- | --- |
| NAT | NeMo regex rail blocks credentials on a rolling window of chunks; **Presidio** masks PII, which forces the whole answer to be buffered first |
| Rig | The same secret patterns in a bounded streaming window (320 characters held back, 512 of look-behind); **deterministic** PII recognisers (regex + Luhn, IBAN mod-97, Base58Check, Bech32) masked inside the same window ([`output.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/guardrails/output.rs), [`pii.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/guardrails/pii.rs)) |
| Relation | secrets **equivalent**; PII **narrower**; latency **different** |
| Takeaway | This is the clearest trade-off in the comparison, and it is not parity. Presidio combines patterns, context and a spaCy NER model; the Rust recognisers are stricter where an entity has a checksum and miss more where it does not (international phone layouts, anything found only through context). What the narrowing buys is a model-free output path that keeps streaming. |

### Tool results shown to people

| | |
| --- | --- |
| NAT | Tool steps reach the UI's tool card and the trace raw |
| Rig | The display copy is redacted (secrets) and masked (PII); the model still reasons over the raw result |
| Relation | **stricter** |
| Takeaway | An output rail that covers only assistant text leaves the tool card as a side channel. |

### Execution state

| | |
| --- | --- |
| NAT | Implicit in NAT's runner |
| Rig | An explicit enum with legal transitions ([`state.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/agent/state.rs)): `Received → ScreeningInput → Running ⇄ AwaitingApproval → Completed / Failed / Cancelled`, `ScreeningInput → Blocked` |
| Relation | **different** |
| Takeaway | Making invalid states unrepresentable — a blocked request cannot await approval, a finished run cannot resume — is cheap in Rust and worth copying into any runtime's design, even when the framework hides its own state. |

### Human-in-the-loop

| | |
| --- | --- |
| NAT | NAT's interaction layer pauses the workflow coroutine; [`interaction_guard.py`](https://github.com/cognokratos/simple-agent-template/blob/main/agent/src/nat_streaming_react/interaction_guard.py) adds owner and offered-choice checks; [`approval.py`](https://github.com/cognokratos/simple-agent-template/blob/main/agent/src/nat_streaming_react/approval.py) prompts and mints |
| Rig | [`approval/gate.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/approval/gate.rs) suspends the run on a `PendingTicket`; [`approval/pending.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/approval/pending.rs) checks the answer and only then builds a `VerifiedDecision` (private constructor); [`approval/token.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/approval/token.rs) mints the same token |
| Relation | **equivalent** security properties; **stricter** lifecycle (prompts expire; partial enablement refuses to start; the model never sees `actor_id`) |
| Takeaway | Same token, same MCP verifier, same single-use transaction. The difference is where the checks live: patched into a toolkit's store, or built into the type the run waits on. |

### Persistence of a suspended run

| | |
| --- | --- |
| NAT | In memory: a paused coroutine holding the client's stream |
| Rig | In memory: an awaiting task holding the client's stream; Rig's `AgentRun` is serialisable, which is the piece a durable design would build on (not implemented) |
| Relation | **equivalent** (both lose pending approvals on restart) |
| Takeaway | Durable suspension needs more than a serialisable run: a reconnectable stream and a pending-record store. Part II of the curriculum covers it. |

### Tracing

| | |
| --- | --- |
| NAT | NAT's exporter and NeMo Guardrails' OTel spans, joined into one trace by pre-seeding NAT's root span through private NAT attributes |
| Rig | `tracing` + `tracing-opentelemetry`; Rig adopts the request's root span, so its `chat` and `execute_tool` spans nest under it; the export filter is fixed in code because Rig logs whole provider requests at TRACE |
| Relation | **equivalent** goal (one trace per request); **different** mechanism |
| Takeaway | One tracing model in one process removes the hardest problem NAT's observability package solves. |

### Evaluation

| | |
| --- | --- |
| Both | The same harness, datasets, scorers and gates; the run is tagged `provenance.agent.runtime` (`nat` or `rig-rust`) |
| Relation | **equivalent** |
| Takeaway | The suites are the fair comparison. See [EVALUATION.md](EVALUATION.md#comparing-runtimes). |

### Deployment and runtime size

| | |
| --- | --- |
| NAT | Python slim image with NAT, NeMo Guardrails, Presidio and a spaCy model: **2.01 GB** (measured from `main` at `8ed2ceb`) |
| Rig | Distroless image with one binary and its config: **71 MB**; no shell, so in-container checks use the binary's `probe` subcommand |
| Relation | **different** |
| Takeaway | Most of NAT's size is the capability it brings (NER, guardrail flows, integrations). Size alone is not the lesson. |

## Framework-provided behaviour vs explicit code

| Behaviour | NAT gives it to you | Rig leaves it to you |
| --- | --- | --- |
| Agent loop, tool calling, streaming | yes | Rig provides it too |
| Guardrail flows, model-based checks, NER masking | yes (NeMo Guardrails, Presidio) | written by hand |
| Interaction/pause protocol | yes (with gaps closed by `main`'s guard) | written by hand |
| Tool policy as one decision point | no — spread over configuration and parsing | written by hand, and therefore explicit |
| Execution state | hidden | written as an enum |
| Profiling, evaluation and plugin ecosystem | yes | no |

## Deliberate behavioural differences

Every difference between the two agents, in one list. None is claimed to be
equivalent.

| Area | NAT (`main`) | Rig (`rust-agent`) | Relation |
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

## What was measured

Single runs on a local stack (`qwen3:8b` on Ollama, both models the same) —
observations, not benchmarks:

| | Rig (`rust-agent`) |
| --- | --- |
| "Show me the open support tickets" | `search_tickets{status: open}`, 15.6 s through the UI |
| "Which ticket should we handle first, and why?" | one `search_tickets` call, TKT-1002 chosen, 3.2 s |
| "Summarize ticket TKT-1003 and its history" | `get_ticket{TKT-1003}`, grounded, 8.4 s |
| Input-rail classifier call | 0.2–2.3 s |
| `make eval-all` | all four suites at their gates (1.0) |
| Agent image | 71 MB (NAT: 2.01 GB) |

[CONFIGURATION.md](CONFIGURATION.md#qwen38b-and-the-prioritization-prompt)
records that `qwen3:8b` struggles with the prioritisation prompt on NAT; on Rig
it answered from `search_tickets` alone in the runs observed. One run is not
evidence that the runtime caused it: the model and prompt are the same, but the
two clients build different request bodies (tool-schema rendering, message
structure).

## Where each is the better tool

* **NAT** when you want breadth now: managed guardrail flows with model-based
  checks, Presidio's NER, a plugin ecosystem, profiling and evaluation
  integrations, and a Python team. You pay in dependency weight and in
  behaviour that lives in framework internals — `main` needs compatibility
  shims and private-attribute access to get one trace.
* **Rig** when you want the loop as a library inside code you own: a small
  dependency surface, compile-time-checked state, a single binary, and every
  security decision as ordinary code. You pay by writing what NAT gives you,
  and by tracking a younger framework whose API moved substantially between
  minor versions.

Either way, the boundary that matters most does not move: the MCP server, not
the agent, decides what may change in the database.

## Further reading

* [AGENT-SERVICE-CONTRACT.md](AGENT-SERVICE-CONTRACT.md) — the boundary both satisfy
* [RUST-LEARNING-PATH.md](RUST-LEARNING-PATH.md) — the Rig implementation as a learning extension
* [RUST-AGENT-ANALYSIS.md](RUST-AGENT-ANALYSIS.md) — how the NAT agent's responsibilities were mapped before the Rust one was written
* [RUST-BRANCH-MAINTENANCE.md](RUST-BRANCH-MAINTENANCE.md) — how `rust-agent` tracks `main`
