# 1. The LLM, the agent, and the agent loop

> Agentic AI is software engineering around a probabilistic decision-making
> component.

This page covers learning-path stages 0 and 1. It explains what the model is,
what an agent adds around it, and how the loop in this repository runs.

## The LLM is a probabilistic component

Treat the model like an unreliable remote service whose output is a *sample*,
not a return value:

| Property | What it means for engineering |
| --- | --- |
| Non-deterministic | The same prompt can produce different answers. `temperature: 0.0` in [`agent/config.yml`](../../agent/config.yml) narrows the spread but does not make output reproducible across model versions, hardware or providers. |
| Untyped output | It emits text (or a structured tool-call request). Anything downstream must parse and validate it. |
| Stateless | It remembers nothing between calls. Every call includes the whole conversation (bounded here by `max_history: 20`). |
| No access to your systems | It knows only its training data and what you put in the prompt. It cannot see your database. |
| Fluent when wrong | A wrong answer is as confident and well-formatted as a right one. |

Two consequences shape everything else in this repository:

1. **You cannot unit-test a model into correctness.** You *measure* it with
   evaluations ([concept 5](05-evaluation.md)) and *observe* it with traces
   ([concept 6](06-observability.md)).
2. **You cannot let it hold authority.** Identity, authorization and state
   changes are enforced by deterministic code that does not depend on what the
   model said ([concept 7](07-security-and-trust-boundaries.md)).

The split this repository is built around:

| Probabilistic: the model decides | Deterministic: code enforces |
| --- | --- |
| interpreting the request | authentication (Keycloak, gateway sessions) |
| planning and reasoning | authorization and the allowed capability surface |
| which tool to call, with which arguments | tool input schemas and parameterized SQL |
| natural-language generation | output regex blocking, PII masking |
| recommendations | policy evaluation, approval-token verification |
| | database constraints, append-only audit records |
| | evaluation assertions, network boundaries |

## What an agent is

An **agent** is a program that uses an LLM to decide its *next action* in a
loop, executes that action through code it controls, and feeds the result back
to the model until the model produces a final answer.

The LLM never executes anything. It produces a *request*: "call `get_ticket`
with `{"ticket_id": "TKT-1001"}`". The **agent runtime** decides whether to
honour that request, executes it, and returns the result as more input.

### Diagram A: the agent mental model

```mermaid
flowchart LR
    U([User]) -->|question| A[Agent runtime]
    A -->|conversation + tool schemas| L{{LLM}}
    L -->|"tool-call request<br/>(name + JSON args)"| A
    A -->|executes| T[Tool]
    T -->|reads| E[(Environment:<br/>database, APIs)]
    E --> T
    T -->|result as text| A
    L -->|final answer| A
    A -->|answer| U

    classDef prob fill:#fde68a,stroke:#b45309,color:#000
    classDef det fill:#bfdbfe,stroke:#1d4ed8,color:#000
    class L prob
    class A,T,E det
```

Yellow is probabilistic, blue is deterministic. The model sits *inside* a loop
that ordinary code owns.

| Concept | Implementation in this repo |
| --- | --- |
| Agent runtime | A Rust service on the [Rig](https://github.com/0xPlaygrounds/rig) agent framework: [`builder.rs`](../../agent/src/agent/builder.rs) assembles the agent, [`execution.rs`](../../agent/src/agent/execution.rs) runs it |
| LLM | Any OpenAI-compatible endpoint (`LLM_BASE_URL` / `LLM_MODEL`, see [CONFIGURATION.md](../CONFIGURATION.md#model-endpoint)); default `qwen3:8b` on a local Ollama |
| Tools | Two read-only MCP tools, `search_tickets` and `get_ticket`, in [`mcp-server/src/main.rs`](../../mcp-server/src/main.rs) |
| Environment | PostgreSQL, schema in [`db/init.sql`](../../db/init.sql) |

## ReAct and native tool calling

**ReAct** ("reason + act") is the loop pattern: the model alternates between
reasoning about what to do and requesting an action, and each observation
(tool result) informs the next step.

Early ReAct implementations ran over plain text. The model wrote
`Action: get_ticket` / `Action Input: {...}` / `Final Answer: ...` and a parser
extracted them with regexes. **Native tool calling** moves this into the model
API: the tool schemas go into the request as structured definitions, and the
model returns a structured `tool_calls` field instead of prose to be parsed.

This repository uses native tool calling: Rig sends the MCP tool schemas as
structured definitions and reads structured tool calls back. Native calling
removes a class of parse failures, and there is no `Final Answer:` marker to
wait for: the answer is simply the assistant text Rig streams once the model
stops calling tools.

### Diagram B: one tool-calling turn

This is what happens for *"Show me the complete details and history for ticket
TKT-1001"*, as observed in a live trace (see the
[request walkthrough](../tutorials/REQUEST-WALKTHROUGH.md)):

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant RT as Agent runtime (Rig loop)
    participant LLM
    participant MCP as MCP server (Rust)
    participant DB as PostgreSQL

    User->>RT: "Show me ... ticket TKT-1001"
    RT->>LLM: system prompt + tool schemas + user message
    LLM-->>RT: tool_call get_ticket {"ticket_id":"TKT-1001"}
    Note over RT: Tool policy decides,<br/>then the runtime executes it.<br/>The model never does.
    RT->>MCP: tools/call get_ticket (Bearer MCP_API_KEY)
    MCP->>DB: SELECT ... WHERE id = $1 (parameterized)
    DB-->>MCP: ticket row + history rows
    MCP-->>RT: JSON text result
    RT->>LLM: conversation + tool result
    LLM-->>RT: final answer (streamed)
    RT-->>User: answer
```

## The loop is bounded by configuration

An unbounded loop around a probabilistic component is an outage waiting to
happen. The bounds live in [`agent/config.yml`](../../agent/config.yml):

| Setting | Value | What it bounds |
| --- | --- | --- |
| `workflow.max_tool_calls` | `20` | Tool calls per request, exactly. Rig allows `max_tool_calls + 1` model calls, and the tool-policy hook ([`hooks.rs`](../../agent/src/agent/hooks.rs)) refuses any tool call beyond the budget. When the loop runs out, the user gets "The agent could not produce a final answer within 20 tool calls" instead of a hang. |
| `workflow.max_history` | `20` | Messages carried into each model call |
| `workflow.max_tokens` | `4096` | Each model reply |
| `tools.mcp.tool_call_timeout_seconds` | `30` | Each MCP call |

A tool error does not crash the run: it goes back to the model as the tool's
result, an *observation* the model can explain. Try `TKT-9999` (scenario 4 in
[TEST-SCENARIOS.md](../TEST-SCENARIOS.md#4-tool-error-handling)).

The loop also has explicit states — received, screening input, running,
awaiting approval, completed, blocked, failed, cancelled — in
[`state.rs`](../../agent/src/agent/state.rs). Every legal move is listed, and
anything else is refused, so a blocked run cannot be resumed and a completed
one cannot start waiting for an approval.

## The system prompt is configuration, not a control

The `workflow.system_prompt` in [`agent/config.yml`](../../agent/config.yml) tells the
model how to use the tools, to ground every factual statement, and to treat
ticket text as untrusted data. It is worth writing carefully, because it
measurably changes behaviour. It is **not** a security boundary. A prompt is a
request to a probabilistic component, and the evaluation suites exist because
"the prompt says not to" is not evidence that it doesn't. See
[ANTI-PATTERNS.md](ANTI-PATTERNS.md#treating-prompts-as-authorization).

## When not to use an agent

If the sequence of steps is known in advance, write it as code. An agent pays
for flexibility with latency, cost and non-determinism. That trade is worth it
when the *path* depends on interpreting natural language ("what's going on with
this customer's orders?"). It is not worth it for "every night, export open tickets to
CSV", and not for any single decision with an explicit rule, such as ranking
tickets by priority ([concept 3](03-grounding-and-authoritative-state.md#grounded-is-not-the-same-as-correct)).
See [ANTI-PATTERNS.md](ANTI-PATTERNS.md#overusing-agents-for-deterministic-workflows).

## The framework runs the loop; it does not own it

Rig is the agent runner here. A Rig agent is assembled **per request** in
[`builder.rs`](../../agent/src/agent/builder.rs), with tools that close over
that request's trusted context, and
[`execution.rs`](../../agent/src/agent/execution.rs) walks one request through
every step, labelling each decision probabilistic or deterministic. The loop is
where the model *decides*, never where authority is *granted*: identity, the
tool budget, the tool policy and the output policy are this repository's code,
which is why the agent could move from the NeMo Agent Toolkit to Rig without
changing them. → [Agent internals, lesson 03](../AGENT-INTERNALS.md#03--understand-the-agent-loop),
[Framework migration — the agent loop](../FRAMEWORK-MIGRATION.md#the-agent-loop)

## Go deeper

* Lab: [01 — Run the agent](../tutorials/01-run-the-agent.md),
  [02 — Understand tool calling](../tutorials/02-understand-tool-calling.md)
* Walkthrough: [Follow one request](../tutorials/REQUEST-WALKTHROUGH.md)
* Reference: [ARCHITECTURE.md](../ARCHITECTURE.md),
  [CONFIGURATION.md — model endpoint](../CONFIGURATION.md#model-endpoint)
* Next concept: [2. Tools and MCP](02-tools-and-mcp.md)
