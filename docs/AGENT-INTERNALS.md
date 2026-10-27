# Inside the agent: thirteen lessons in Rust

An advanced appendix to the [learning path](LEARNING-PATH.md). Take it **after**
the core path: it assumes you already know the architecture — gateway, agent,
MCP capability boundary, guardrails, evaluation, observability, human approval —
from the concept pages and labs.

The core path teaches *why* each component exists. This appendix opens the
agent service itself ([`agent/`](../agent/)) and follows the machinery an agent
framework usually hides — the loop, tool dispatch, policy, streaming,
suspension, resumption — as Rust code you can read, change and test. Rig runs
the loop; everything that decides authority is ordinary code in this crate.
Every lesson ends on the invariant the agent must satisfy, whatever framework
runs it.

> The model proposes actions. Deterministic software decides what authority is
> actually exercised.

It is not a Rust course. Rust concepts appear only where they carry an
agent-engineering idea: enums as state machines, ownership across `await`,
`Arc` for shared request context, async streams, traits, typed errors,
serialisation, and visibility as a security property.

Each lesson also notes, under **What NAT did instead**, how the retired NeMo
Agent Toolkit implementation (tag `nat-1.9-final`) handled the same concern —
the clearest way to see what a high-level toolkit does for you, and what it
hides. The full comparison is [FRAMEWORK-MIGRATION.md](FRAMEWORK-MIGRATION.md).

## Setup

```bash
git clone https://github.com/cognokratos/simple-agent-template
cd simple-agent-template/agent
cargo test          # Rust ≥ 1.95; no model, cluster or network needed
```

The integration tests run the real agent service against a fake
OpenAI-compatible model and an in-process MCP server
([`tests/support/mod.rs`](../agent/tests/support/mod.rs)), so every exercise
until lesson 13 runs offline.

## The lessons at a glance

| # | Lesson | Source | What NAT did instead (`nat-1.9-final`) |
| --- | --- | --- | --- |
| 01 | Call an LLM from Rust | [`agent/model.rs`](../agent/src/agent/model.rs) | LangChain client via [`llm_config.py`](https://github.com/cognokratos/simple-agent-template/blob/nat-1.9-final/agent/src/nat_streaming_react/llm_config.py) |
| 02 | Build an agent using Rig | [`agent/builder.rs`](../agent/src/agent/builder.rs) | NAT workflow config in `agent/config.yml` |
| 03 | Understand the agent loop | [`agent/execution.rs`](../agent/src/agent/execution.rs) | NAT ReAct graph, [`register.py`](https://github.com/cognokratos/simple-agent-template/blob/nat-1.9-final/agent/src/nat_streaming_react/register.py) |
| 04 | Discover tools through MCP | [`mcp/client.rs`](../agent/src/mcp/client.rs) | NAT `mcp_client` function group |
| 05 | Validate model-proposed tool arguments | [`mcp/schema.rs`](../agent/src/mcp/schema.rs) | pydantic models from MCP schemas |
| 06 | Separate model proposals from software authority | [`agent/hooks.rs`](../agent/src/agent/hooks.rs), [`guardrails/tools.rs`](../agent/src/guardrails/tools.rs) | NAT tool configuration |
| 07 | Stream output safely | [`guardrails/output.rs`](../agent/src/guardrails/output.rs), [`api/workflow.rs`](../agent/src/api/workflow.rs) | NAT streaming + NeMo regex rail |
| 08 | Implement deterministic guardrails | [`guardrails/input.rs`](../agent/src/guardrails/input.rs), [`guardrails/pii.rs`](../agent/src/guardrails/pii.rs) | [`text_guardrails.py`](https://github.com/cognokratos/simple-agent-template/blob/nat-1.9-final/agent/src/nat_streaming_react/text_guardrails.py), Presidio |
| 09 | Propagate trusted identity outside the prompt | [`identity.rs`](../agent/src/identity.rs), [`api/auth.rs`](../agent/src/api/auth.rs) | [`fastapi_worker.py`](https://github.com/cognokratos/simple-agent-template/blob/nat-1.9-final/agent/src/nat_streaming_react/fastapi_worker.py) |
| 10 | Model execution as an explicit state machine | [`agent/state.rs`](../agent/src/agent/state.rs) | implicit in NAT's runner |
| 11 | Suspend for human approval | [`approval/gate.rs`](../agent/src/approval/gate.rs), [`agent/scope.rs`](../agent/src/agent/scope.rs) | NAT interaction layer, [`approval.py`](https://github.com/cognokratos/simple-agent-template/blob/nat-1.9-final/agent/src/nat_streaming_react/approval.py) |
| 12 | Resume and execute safely | [`approval/pending.rs`](../agent/src/approval/pending.rs), [`approval/token.rs`](../agent/src/approval/token.rs) | [`interaction_guard.py`](https://github.com/cognokratos/simple-agent-template/blob/nat-1.9-final/agent/src/nat_streaming_react/interaction_guard.py) |
| 13 | Trace and evaluate the whole lifecycle | [`telemetry/mod.rs`](../agent/src/telemetry/mod.rs), [`tests/`](../agent/tests) | NAT `observability/`, the shared evaluator |

---

## 01 — Call an LLM from Rust

**Rig exposes.** An OpenAI-compatible endpoint is a base URL, a key and a
model name: `OpenAIConfig::with_base_url(..).client().chat(model)`. `classify`
in [`model.rs`](../agent/src/agent/model.rs) is the simplest possible model
call — one request, one reply — and it is the input rail's classifier. Note what
it does *not* do: parse. The reply is text; deciding what it means is
`classifier_says_safe`, a pure function.

**What NAT did instead.** A configured provider (`llms.primary` in NAT's config) that
NAT injected into its workflow, plus a local provider so that an empty
`reasoning_effort` was omitted rather than sent.

**Rust that matters.** `Result` at an I/O boundary: `classify` returns
`Result<String, GuardModelError>`, and the caller decides that a failure means
*refuse* (fail closed).

**Invariant.** A model reply is data to be interpreted by deterministic code,
never a decision in itself.

**Exercise.** In `tests/agent_loop.rs`, `an_unreadable_verdict_fails_closed`
sets the classifier's reply to `"<think>hmm"`. Change it to `"Not safe"`,
predict the outcome from `classifier_says_safe`'s documentation, and run
`cargo test --test agent_loop unreadable`. Explain why that known parser hazard
is unreachable in practice — the same three reasons applied on NAT
([GUARDRAILS.md](GUARDRAILS.md#what-the-llm-verdict-actually-is)).

## 02 — Build an agent using Rig

**Rig exposes.** A Rig `Agent` is a model, a preamble, sampling settings, a turn
budget and a set of tools. The Rig agent is built **once per request**
([`builder.rs`](../agent/src/agent/builder.rs)) because building one spawns
nothing, and because each tool can then close over that request's trusted
context (lesson 09).

**What NAT did instead.** One workflow assembled at startup from `config.yml`; the
request's context reached functions through NAT's `Context`.

**Rust that matters.** A typestate builder (`AgentBuilder` changes type when
tools are added) and `Arc<RequestScope>` shared among tool closures.

**Invariant.** What a tool can reach is fixed before the model produces a
single token.

**Exercise.** Add `.temperature(0.7)` in `build_agent`, run
`cargo test --test agent_loop a_valid_call`, and inspect what the fake model
received (`agent.llm.agent_requests()[0]["temperature"]`). Revert.

## 03 — Understand the agent loop

**Rig exposes.** Call the model; if it asked for tools, run them and call again;
stop at an answer or the budget. Rig implements this as a **serialisable,
sans-I/O state machine** (`AgentRun`: `CallModel`, `CallTools`, `Done`) driven
by a runner. [`execution.rs`](../agent/src/agent/execution.rs) walks one
request through it, step by step, labelling each decision probabilistic or
deterministic. Read `AgentRun::next_step` (`cargo doc -p rig-agent --open`) to
see exactly what the runner does between your hooks.

**What NAT did instead.** The same loop as a LangGraph ReAct graph, configured by
`max_tool_calls` and friends ([concept 1](concepts/01-agents-and-agent-loops.md)).

**Rust that matters.** Enums as protocol state (`AgentRunStep`).

**Invariant.** The loop is bounded by configuration, not by the model deciding
to stop.

**Exercise.** `the_loop_is_bounded_by_configuration` scripts a model that never
stops calling tools. Set `max_tool_calls: 3` in `agent/config.yml`, run it, and
explain the two limits involved: Rig's model-call budget and the hook's
tool-call budget. Why was the second one needed? Revert.

## 04 — Discover tools through MCP

**Rig exposes.** At startup the agent asks the MCP server for its tools, keeps
the allow-listed ones, overrides descriptions from config, and **refuses to
start** if a schema uses anything it cannot enforce or declares an argument
such as `user_id` ([`client.rs`](../agent/src/mcp/client.rs), `select_tools`).
Invocation goes through `rig-rmcp`'s `McpTool` over `rmcp`.

**What NAT did instead.** The `mcp_client` function group: discovery, `include:`,
`tool_overrides`, reconnect — configuration rather than code.

**Rust that matters.** A pure function (`select_tools`) separated from the I/O
that feeds it, so the policy is unit-testable without a server.

**Invariant.** MCP is the capability boundary; the agent has no other route to
the data.

**Exercise.** In the fake MCP server (`tests/support/mod.rs`), add a
`requester_email: Option<String>` field to `GetTicketArgs`. Run any integration
test and read the startup error. What would have happened downstream had it
been accepted?

## 05 — Validate model-proposed tool arguments

**Rig exposes.** [`schema.rs`](../agent/src/mcp/schema.rs) turns each published
JSON Schema into a closed, typed representation and checks every proposed
argument object against it: unknown fields refused even without
`additionalProperties: false`, nothing coerced (`{"limit": 5.0}` is not an
integer), unsupported keywords refused at discovery.

**What NAT did instead.** Arguments parsed into pydantic models, which coerced where
pydantic coerces.

**Rust that matters.** `serde_json::Value` at the untrusted edge, typed
structures inside.

**Invariant.** The model gains no authority by producing convincing JSON; the
MCP server still re-validates and stays authoritative.

**Exercise.** Run `cargo test --test agent_loop malformed_and_mistyped` and add
a case for `{"ticket_id": "TKT-1001", "ticket_id ": "x"}` (a key with a trailing
space). Predict the decision first.

## 06 — Separate model proposals from software authority

**Rig exposes.** Rig calls `AgentHook::on_dispatch` before every tool runs, with
the name and raw argument text the model produced.
[`hooks.rs`](../agent/src/agent/hooks.rs) asks one pure function,
[`ToolPolicy::decide`](../agent/src/guardrails/tools.rs): `Allow` → proceed,
`RequireApproval` → proceed only into the approval gate, `Deny` → skip with a
reason the model sees. The executors check again.

**What NAT did instead.** The set of callable tools was NAT configuration; there was no
single decision point between "the model asked" and "the tool ran".

**Rust that matters.** An exhaustive `match` on a decision enum: add a variant
and the compiler lists every place that must handle it.

**Invariant.** The decision whether a proposed call runs is deterministic, and
deciding whether a *mutation is permitted* stays with the MCP server.

**Exercise.** Add a `ToolEffect::Forbidden` variant that `decide` always
refuses, and follow the compiler errors to completion. Then delete it.

## 07 — Stream output safely

**Rig exposes.** Rig yields stream items; only text fragments are candidates
for the client. The run executes in its own task and writes typed wire events
into a bounded channel; the HTTP body is a stream over the receiver
([`workflow.rs`](../agent/src/api/workflow.rs)). Dropping the body aborts the
run. Before release, each fragment passes the output guard
([`output.rs`](../agent/src/guardrails/output.rs)), which holds back the last
320 characters and scans them with 512 of look-behind, so a secret split across
fragments is seen whole before any of it leaves.

**What NAT did instead.** NAT's stream through the guardrail middleware; NeMo's regex
rail evaluated a rolling window of chunks. Presidio PII masking forced the
whole answer to be buffered ([GUARDRAILS.md](GUARDRAILS.md)).

**Rust that matters.** Async streams, bounded channels for back-pressure, RAII
cancellation (`Drop`).

**Invariant.** Nothing reaches the client that the output policy has not
released — across chunk boundaries.

**Exercise.** Run `long_answers_stream_progressively` and
`a_secret_split_across_chunks_is_blocked_before_any_part_is_released`. Set
`HOLDBACK_CHARS` to `4` and run them again. What does the hold-back cost, and
what does it buy?

## 08 — Implement deterministic guardrails

**Rig exposes.** The input policy as pure functions
([`input.rs`](../agent/src/guardrails/input.rs)) — the same deny patterns,
anchored allow templates and asymmetric precedence the NAT agent used — plus one
classifier call. The output PII recognisers ([`pii.rs`](../agent/src/guardrails/pii.rs))
are regex tightened with checksums.

**What NAT did instead.** NeMo Guardrails flows and Presidio's NER. Presidio found
entities the Rust recognisers miss; the Rust recognisers need no model and fit
the streaming window. That is a **narrower** guarantee, not parity
([FRAMEWORK-MIGRATION.md](FRAMEWORK-MIGRATION.md#output-guardrails-and-pii)).

**Rust that matters.** `LazyLock<Regex>` compiled once; table-driven tests.

**Invariant.** Every guardrail decision records which layer made it, and only
the classifier's reply is probabilistic.

**Exercise.** Add a test splitting a key across three chunks after a long
preamble, and one interleaving a PII entity with a secret. Then compare with
[lab 07](tutorials/07-experiment-with-guardrails.md) against the running stack.

## 09 — Propagate trusted identity outside the prompt

**Rig exposes.** [`TrustedCaller`](../agent/src/identity.rs) has no public
constructor: the only code that builds one reads gateway headers after the
service key is verified ([`auth.rs`](../agent/src/api/auth.rs)). It is never
rendered into the prompt, no tool schema has a field for it, and the approval
token takes its `actor_id` from it.

**What NAT did instead.** The same two checks as ASGI middleware in
[`fastapi_worker.py`](https://github.com/cognokratos/simple-agent-template/blob/nat-1.9-final/agent/src/nat_streaming_react/fastapi_worker.py), and
identity read from NAT's request metadata.

**Rust that matters.** Visibility (`pub(crate)`) as a security property; a
redacting `Debug` so a stray `{:?}` cannot log the subject.

**Invariant.** The model cannot choose, see or forge the identity that
authorisation and audit use.

**Exercise.** Try to construct a `TrustedCaller` in an integration test and read
the compiler error. Then explain what
`identity_never_enters_the_model_context_or_a_tool_call` checks that the type
system alone does not.

## 10 — Model execution as an explicit state machine

**Rig exposes.** [`state.rs`](../agent/src/agent/state.rs):

```text
Received → ScreeningInput → Running ⇄ AwaitingApproval → Completed / Failed / Cancelled
                         ↘ Blocked
```

Every legal move is listed in one `match`; anything else is refused. A blocked
request cannot await approval, a finished run cannot resume, input screening
cannot be skipped.

**What NAT did instead.** The same lifecycle, implicit in NAT's runner and its execution
store.

**Rust that matters.** Enums with data (`AwaitingApproval { interaction_id }`)
make invalid states hard to represent; transitions return `Result`.

**Invariant.** How a run ended is a fact recorded by code, not inferred from
logs.

**Exercise.** Run `cargo test --lib agent::state`. Add a transition
`Completed → Running` and write the test that should reject it.

## 11 — Suspend for human approval

**Rig exposes.** The model proposes `ticket_priority_change`.
[`gate.rs`](../agent/src/approval/gate.rs) opens a pending interaction, sends
`interaction_required`, and **awaits** a `PendingTicket`
([`scope.rs`](../agent/src/agent/scope.rs), `ask`). The whole run is suspended
inside that `.await`, holding the client's stream — the same shape as NAT's
paused coroutine was. Nothing is persisted; see [LIMITATIONS.md](LIMITATIONS.md).

**What NAT did instead.** `prompt_user_input` in NAT's interaction layer, used by
[`approval.py`](https://github.com/cognokratos/simple-agent-template/blob/nat-1.9-final/agent/src/nat_streaming_react/approval.py).

**Rust that matters.** `tokio::sync::oneshot` as a single-use rendezvous;
`Drop` to remove an abandoned prompt.

**Invariant.** A suspended run can be resumed only through the authenticated
interaction route.

**Exercise.** Run `cargo test --test approvals a_disconnect_while_waiting` and
`an_unanswered_prompt_expires`, and draw the state transitions for each.

## 12 — Resume and execute safely

**Rig exposes.** [`InteractionRegistry::respond`](../agent/src/approval/pending.rs)
checks that the interaction exists and is pending, the responder owns it, the
response kind matches the prompt, and the `(id, value)` pair was offered — and
only then builds a `VerifiedDecision`, a type nothing else can construct. The
token ([`token.rs`](../agent/src/approval/token.rs)) is minted from that
decision plus the trusted caller, and the MCP server verifies it independently.

**What NAT did instead.** NAT resolved an interaction on two UUIDs alone; the NAT tree's
[`interaction_guard.py`](https://github.com/cognokratos/simple-agent-template/blob/nat-1.9-final/agent/src/nat_streaming_react/interaction_guard.py)
substituted NAT's execution store to add the same checks.

**Rust that matters.** "Parse, don't validate": an unforgeable type as proof
that a check happened.

**Invariant.** The model cannot manufacture approval; the MCP server is the
final authority on the mutation.

**Exercise.** Run `cargo test --test approvals` and read
`only_the_owner_can_answer_and_only_with_what_was_offered`. Then run
`cargo test --lib against_the_mcp_verifier`, which compiles the MCP server's own
verifier source into the agent's tests. Swap the key order in `canonical_json`
and watch which tests fail.

## 13 — Trace and evaluate the whole lifecycle

**Rig exposes.** One root span per request; everything else — the rails, Rig's
own `chat` and `execute_tool` spans, `tool.policy`, the MCP call, the approval
wait — is its child because it is created while the root is current
([`telemetry/mod.rs`](../agent/src/telemetry/mod.rs)). The export filter is fixed
in code, so raising `RUST_LOG` cannot export Rig's raw request log. The test
suites ([`tests/`](../agent/tests)) play the model as an adversary: unknown
tools, invented `user_id`s, forged approval fields, split secrets, a loop that
never ends.

**What NAT did instead.** NAT's spans and NeMo's spans joined into one trace by a local
observability package relying on private NAT attributes; the same evaluation
harness.

**Rust that matters.** `tracing::Instrument` to carry a span across `.await` and
`tokio::spawn`.

**Invariant.** One trace per request, without credentials or raw identity; the
boundary is tested, not the model.

**Exercise.** Run `cargo test --test observability`. Then bring up the stack
(`make dev`), run `make eval-all`, and find the run in MLflow by its
`provenance.agent.runtime` tag ([EVALUATION.md](EVALUATION.md#comparing-runtimes)).
Open the trace of one prompt and match each span to the lesson that created it
([request walkthrough](tutorials/REQUEST-WALKTHROUGH.md)). For the framework
comparison, repeat the evaluation at the `nat-1.9-final` tag and compare the
two runs by that tag.

## Upgrading Rig

Rig, `rig-agent`, `rig-rmcp`, `rmcp` and the OpenTelemetry crates are pinned
exactly in [`agent/Cargo.toml`](../agent/Cargo.toml), and
`scripts/verify_security_sources.py` fails if the Rig crates are not. Rig
reshapes its API between minor versions. To upgrade, change the pins together,
run `make agent-check`, and re-read the release notes for hooks
(`AgentHook::on_dispatch`, `on_invalid_tool_call`), streaming items
(`MultiTurnStreamItem`) and tool context — the three APIs the policy layer
depends on. Commit the updated `agent/Cargo.lock` with the pins: CI builds
with `--locked`.
