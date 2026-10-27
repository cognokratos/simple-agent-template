# Known limitations and untested behaviour

Stated rather than implied. A control that is documented but unverified is worse
than one that is absent, because it is believed.

## Not tested automatically

| Behaviour | Why not | How to check by hand |
| --- | --- | --- |
| Nonce conflict under real concurrency | Needs a live PostgreSQL; the constraint is a primary key, enforced by the database | Two concurrent spends of one approval token against a running cluster |
| Rollback of a failed audit insert | Same | Break the audit insert and confirm the status is unchanged and the nonce free |
| End-to-end approval through the browser | Needs a cluster, a model that calls the function, and a human | Enable the feature and follow [APPROVALS.md](APPROVALS.md) |
| Keycloak login through a real browser | Needs the cluster | `make dev`, then sign in |
| The evaluation suites' actual scores | Non-deterministic and model-dependent | `make eval-all` with a model available |
| Trace export reaching MLflow | Needs the cluster and a model | `make trace-test` |

The `make` targets above exist and are documented; they are simply not part of
any automated gate.

## Deliberate gaps

The full list of where this agent behaves differently from the retired NAT
agent (`nat-1.9-final`) is in
[FRAMEWORK-MIGRATION.md](FRAMEWORK-MIGRATION.md#deliberate-behavioural-differences).

**`workflow.request_timeout_seconds` is not enforced.** It is parsed and
validated from `agent/config.yml`, but nothing applies it, so a run has no
overall wall-clock bound. The run is bounded by the tool-call budget, the
per-tool `tools.mcp.tool_call_timeout_seconds`, the pending-approval timeout and
the client disconnecting, but a model endpoint that streams indefinitely is not
cut off by the agent. Enforcing it is a behaviour change left out of the
framework promotion and tracked as a follow-up.

**A fabricated prior *user* turn is not screened.** The input rail screens the
latest turn and any client-supplied *assistant* turn. It does not re-screen prior
user turns, because doing so made one refusal poison the rest of a conversation.
The same caller can send that text as the latest turn, where the full rail does
screen it. See [GUARDRAILS.md](GUARDRAILS.md).

**PII detection is narrower than NER.** Seven entity types, from deterministic
recognisers (regex tightened with Luhn, IBAN mod-97, Base58Check and Bech32) in
[`guardrails/pii.rs`](../agent/src/guardrails/pii.rs), not a context-scored NER
model such as the Presidio analyser the retired NAT agent used. Stricter where
an entity has a checksum, weaker where it does not — phone numbers in layouts
other than international `+CC …` and North American `3-3-4`, and anything an NER
model finds through context. Not parity with Presidio, and not claimed as such.

**The streaming window has a bound.** Secrets are caught across chunk
boundaries by holding back 320 characters and scanning them with 512 of
look-behind ([`guardrails/output.rs`](../agent/src/guardrails/output.rs)). A
match whose own span exceeds that can be missed. Answers under 320 characters
arrive in one piece, and an answer longer than
`GUARDRAILS_PII_MAX_BUFFER_CHARS` stops with a notice.

**Header redaction is not content redaction.** Credential-bearing headers are
never exported. Content is redacted only by the output rail's configured secret
patterns and PII recognisers — on the answer, and on tool results shown in the
UI and the trace. A secret none of them matches is not reached. See
[OBSERVABILITY.md](OBSERVABILITY.md).

**Per-user trace attribution is off by default.** The agent exports a per-user
identifier only when `OTEL_TRACE_USER_ID=true`, and then a stable UUIDv5
pseudonym rather than the Keycloak subject
([`TrustedCaller::pseudonym`](../agent/src/identity.rs)). The identifier is
withheld by default because traces may carry the question and the answer, and
the identifier is what turns them from a corpus into a per-person record —
whether that is acceptable depends on the trace store's access controls and
retention. A pseudonym is a weaker disclosure but not anonymity: it is the same
value for the same person on every request. See
[OBSERVABILITY.md](OBSERVABILITY.md).

**Sessions are in memory.** One gateway instance, and a restart logs everyone
out.

**Approvals suspend in memory.** A restart loses pending approvals (a late
answer gets `404`; nothing is applied). Rig's `AgentRun` is serialisable — the
piece a durable design would build on — but durable suspension would also need
a reconnectable stream and a pending-record store, none of which is
implemented. Pending approvals expire (`HITL_INTERACTION_TIMEOUT_SECONDS`).

**One provider verified:** Ollama with `qwen3:8b`, plus a fake OpenAI-compatible
endpoint in the tests.

**Container log content follows `AGENT_RUST_LOG`.** The exported trace is
protected by a filter fixed in code
([`telemetry/mod.rs`](../agent/src/telemetry/mod.rs)); the container's stdout is
not, so raising Rig's log level there writes prompts and tool results to the
log.

**Only the contract's routes exist**, and the image is distroless: no shell, so
in-container checks use the binary's `probe` subcommand (`make shell-agent`
prints how).

## Resource requirements

The full stack runs eight services at once: assistant-ui, the gateway,
Keycloak, PostgreSQL, the MCP server, the agent, the OpenTelemetry Collector and
MLflow. Plan for a Docker VM of about 8 GB. The agent itself is small: a
distroless image with one binary and its configuration, and no NER model or
other in-process ML dependency.

* **The model is not in that budget.** By default the agent calls an
  OpenAI-compatible endpoint on the host (`LLM_BASE_URL`, Ollama with
  `qwen3:8b`), so the model's memory is the host's, not the Docker VM's. A
  hosted endpoint removes it entirely.
* **The first build compiles Rust.** The agent, gateway and MCP server are
  built from source inside Docker, which is the slowest step of the first
  `make dev`.
* **The agent's tests need no Docker.** `make agent-test` and
  `make agent-check` run on the host with a Rust toolchain (≥ 1.95), no cluster
  and no model ([VERIFICATION.md](VERIFICATION.md)).

On a VM near capacity, a container the kernel kills shows up as a truncated
stream or a restart rather than a failing assertion. If `make trace-test` fails
that way, check `docker inspect <container> --format '{{.RestartCount}}'`
across the request before looking for a fault in the agent.

## Dependency constraints

Rig (`rig-core`, `rig-agent`, `rig-rmcp`), `rmcp` and the OpenTelemetry crates
are pinned exactly in [`agent/Cargo.toml`](../agent/Cargo.toml), and
`scripts/verify_security_sources.py` fails if the Rig crates are not. Rig is
young and its API moved substantially between minor versions; upgrading is a
deliberate step ([AGENT-INTERNALS.md](AGENT-INTERNALS.md#upgrading-rig)). No
private Rig API is used.

## Before production

This is a local demonstration. Add:

* authorization and tenant/user scoping in every SQL query — the MCP tools
  currently return any row the query matches;
* secrets management instead of the demo credentials in `docker-compose.yml`;
* database migrations rather than a one-time init script;
* pagination and response-size limits for history-heavy records;
* access controls, retention and redaction for OpenTelemetry and MLflow data;
* a dedicated low-latency guard model rather than sharing the application LLM;
* explicit image digest pinning and vulnerability scanning;
* a session store that survives a restart and supports more than one instance.

## Licensing

Original code and documentation are licensed under **MIT** (root
[`LICENSE`](../LICENSE)). The package metadata (`agent/Cargo.toml`,
`gateway/Cargo.toml`, `mcp-server/Cargo.toml`, `ui/package.json`) says the same.

The agent contains no NVIDIA-derived files. The three Apache-2.0 files the
retired NAT agent adapted from NVIDIA NeMo Agent Toolkit, with their notices
and provenance record, remain at the
[`nat-1.9-final`](https://github.com/cognokratos/simple-agent-template/tree/nat-1.9-final)
tag; consult that tag's `THIRD_PARTY_NOTICES.md` if you fork from it. The
current [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) says so and lists
the dependency licences, including the one data licence (the Mozilla CA bundle
compiled into the agent and gateway binaries). [`LICENSES/Apache-2.0.txt`](../LICENSES/Apache-2.0.txt)
is kept because many linked crates are `MIT OR Apache-2.0` or `Apache-2.0`.

The agent image carries the licence documents under
`/usr/share/doc/tickets-agent/`. `agent/LICENSE`, `agent/LICENSES/Apache-2.0.txt`
and `agent/THIRD_PARTY_NOTICES.md` are byte-identical copies of the root files,
which stay authoritative; they exist because the image build sees only `agent/`.
`make license-check` fails if a copy drifts, and `make package-license-check`
checks the texts inside the built image.
