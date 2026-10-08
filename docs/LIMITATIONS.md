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

A gap marked **(NAT)** is specific to the canonical NAT agent on `main`; the
others apply to both agents. The Rig agent's own gaps and differences follow in
[Rig implementation](#rig-implementation-rust-agent).

**A fabricated prior *user* turn is not screened.** The input rail screens the
latest turn and any client-supplied *assistant* turn. It does not re-screen prior
user turns, because doing so made one refusal poison the rest of a conversation.
The same caller can send that text as the latest turn, where the full rail does
screen it. See [GUARDRAILS.md](GUARDRAILS.md).

**PII masking costs streaming (NAT).** NeMo's streaming rail runner can only use an
action's result to decide blocked/not-blocked, never to rewrite text, so while
`mask sensitive data on output` is enabled the middleware buffers the complete
answer, masks it once and only then releases it
(`TextGuardrailsMiddleware._stream_with_buffered_masking`; see
[GUARDRAILS.md](GUARDRAILS.md)). Answers are masked, but no longer stream token
by token, and one over `GUARDRAILS_PII_MAX_BUFFER_CHARS` is refused. The
configured `score_threshold` is also not honoured by the pinned release's masking
action — the effective floor is Guardrails' hardcoded 0.4. Both are asserted by
`verify_output_guardrails.py` so they cannot drift unnoticed.

**Header redaction is not content redaction.** The telemetry processor removes
credential-bearing headers. A secret inside a tool result or a model answer is
not reached by it. See [OBSERVABILITY.md](OBSERVABILITY.md).

**NAT's own `identity_header` refusal is advisory on the workflow routes (NAT).**
Configured, NAT 1.9 raises `IdentityHeaderError` for a missing, empty or
repeated identity header and registers a handler that would answer `401`. That
handler is not reached: `add_generate_routes` serves the workflow path and its
`/stream` and `/full` variants through the interactive runner unconditionally,
and that runner acquires the session in a background task wrapped in a blanket
`except Exception`, so the caller gets `200` with a `WORKFLOW_ERROR` in the
stream. `RequireIdentityHeaderMiddleware` in `fastapi_worker.py` is what
actually enforces the requirement, and `make auth-test` asserts it. See
[SECURITY.md](SECURITY.md#the-agent-requires-an-asserted-identity).

**Per-user trace attribution is off by default.** Both agents export a per-user
identifier only when `OTEL_TRACE_USER_ID=true`, and then a pseudonym rather than
the Keycloak subject (the Rig agent's differs in value; see
[OBSERVABILITY.md](OBSERVABILITY.md#on-the-rig-implementation)). On NAT: NAT 1.9 stamps every span with
the authenticated user (`user.id` and `nat.user.id`). That is genuinely useful
for triage, and it is withheld unless `OTEL_TRACE_USER_ID=true`, because the
traces already carry the question and the answer — the identifier is what turns
them from a corpus into a per-person record, and whether that is acceptable
depends on the trace store's access controls and retention. The value is a
stable `uuid5` pseudonym rather than the Keycloak subject, which is a weaker
disclosure but not anonymity: it is the same value for the same person on every
request. `UserIdentityProcessor` in `observability/trace_processor.py`. The raw
gateway identity headers NAT copies into span metadata are redacted in both
modes, so the switch governs the only per-user identifier a trace can carry.

**Sessions are in memory.** One gateway instance, and a restart logs everyone
out.

**An interaction with no recorded owner is allowed through (NAT)** unless
`HITL_STRICT_INTERACTION_OWNERSHIP=true`, so NAT's own OAuth consent flow keeps
working. Every interaction the approval module creates *is* recorded.

## Rig implementation (`rust-agent`)

What the Rig agent does not do, or does differently, compared with the
canonical NAT agent. The full comparison is in
[NAT-VS-RIG.md](NAT-VS-RIG.md#deliberate-behavioural-differences).

**PII detection is narrower than Presidio.** The same seven entity types, from
deterministic recognisers (regex tightened with Luhn, IBAN mod-97, Base58Check
and Bech32), not Presidio's context-scored NER. Stricter where an entity has a
checksum, weaker where it does not — phone numbers in layouts other than
international `+CC …` and North American `3-3-4`, and anything Presidio finds
through context. Not parity, and not claimed as such.

**The streaming window has a bound.** Secrets are caught across chunk
boundaries by holding back 320 characters and scanning them with 512 of
look-behind. A match whose own span exceeds that can be missed. Answers under
320 characters arrive in one piece.

**Approvals suspend in memory**, as on NAT: a restart loses pending approvals
(a late answer gets `404`; nothing is applied). Rig's `AgentRun` is
serialisable — the piece a durable design would build on — but durable
suspension would also need a reconnectable stream and a pending-record store,
none of which is implemented. Pending approvals expire
(`HITL_INTERACTION_TIMEOUT_SECONDS`).

**One provider verified:** Ollama with `qwen3:8b`, plus a fake OpenAI endpoint in
the tests.

**Rig is young and pinned exactly**; its API moved substantially between minor
versions ([RUST-BRANCH-MAINTENANCE.md](RUST-BRANCH-MAINTENANCE.md#upgrading-rig)).
No private Rig API is used.

**Container log content follows `RUST_LOG`.** The exported trace is protected
by a fixed filter; the container's stdout is not, so raising Rig's log level
there writes prompts and tool results to the log.

**Only the contract's routes exist**, and the image is distroless: no shell, so
in-container checks use the binary's `probe` subcommand (`make shell-agent`
prints how).

**Resources.** The Rig agent image is 71 MB with no NER model, so the Presidio
memory notes below do not apply to it.

## Resource requirements

On the NAT implementation. (The Rig agent uses no NER model; see
[Rig implementation](#rig-implementation-rust-agent).)

`make verify-output-guardrails` loads Presidio's analyzer, which pulls spaCy's
`en_core_web_lg` into memory — roughly 600 MB on top of the agent's own
footprint. On a Docker VM already near capacity the kernel kills it, which
surfaces as a bare `exit 137` rather than a failing assertion. The script warns
before that point. Give Docker headroom, or run the same script on the host
where the dependencies are installed.

Observed: on a 7.7 GB Docker VM with MLflow at 2 GB and an unrelated stack
running, the masking half was OOM-killed while the configuration, pattern and
wiring halves passed. The same script passed in full on the host.

**This is not confined to the verification script.** Any live request whose
answer reaches the `mask sensitive data on output` flow loads the same analyzer,
so on a VM without that headroom the agent process is SIGKILLed mid-stream while
masking. It leaves no Python-level error — the client sees the intermediate-step
events, then a truncated stream (`curl: (18)`), and the container restarts with
`RestartCount` incremented, `OOMKilled=false` and exit code 0, none of which name
memory as the cause. `make trace-test` fails as "no streamed data chunks were
returned".

Measured on the same 7.7 GB VM: with MLflow running the request was killed every
time; stopping MLflow alone (freeing ~2 GB) made the same request return its
masked answer with no restart. If `make trace-test` fails that way, check
`docker inspect <agent> --format '{{.RestartCount}}'` across the request before
looking for a fault in the agent.

## Dependency constraints

On the NAT implementation. The Rig agent pins its own crates exactly
([RUST-BRANCH-MAINTENANCE.md](RUST-BRANCH-MAINTENANCE.md#upgrading-rig)).

`nvidia-nat-security[guardrails]==1.9.0` pins `nemoguardrails>=0.11,<0.22`, so
0.23.0 — which fixes three streaming rail defects — cannot be installed.
`guardrails_compat.py` works around them from application code and self-disables
once the installed release is correct. Delete it when the pin allows `>=0.23`.

**The 1.9 upgrade did not relax this.** The requirement is byte-identical to
1.8.0's. So are `nemo_guardrails_middleware.py`, `execution_store.py`,
`routes/execution.py` and `nat/llm/openai_llm.py`, and the ReAct `_stream_fn`
still buffers until it sees `Final Answer:`. Every workaround in
`agent/src/nat_streaming_react/` therefore still has a reason to exist after the
upgrade; none became deletable. See [EXTENDING.md](EXTENDING.md) for the
per-module removal conditions.

The observability package relies on three private NAT attributes, each listed
with its removal condition in `observability/__init__.py` and
[OBSERVABILITY.md](OBSERVABILITY.md). This is **not** a purely public-API
implementation, and all three are still private in 1.9.

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
[`LICENSE`](../LICENSE)). The package metadata (`gateway/Cargo.toml`,
`mcp-server/Cargo.toml`, `ui/package.json`) and the SPDX headers of the original
Python sources say the same.

On `main`, three files under `agent/src/nat_streaming_react/` are exceptions. `register.py`
and `text_guardrails.py` are modified from NVIDIA NeMo Agent Toolkit code, and
`observability/otlp_exporter.py` closely follows it. They keep their Apache-2.0
declarations and NVIDIA's copyright notices, which is why `agent/pyproject.toml`
declares `MIT AND Apache-2.0`. The list and the Apache-2.0 text are in
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) and
[`LICENSES/Apache-2.0.txt`](../LICENSES/Apache-2.0.txt). When you fork, keep
those notices with the files they cover.

On `rust-agent` none of those files exist; its `THIRD_PARTY_NOTICES.md` says
so, and the agent image carries the licence documents under
`/usr/share/doc/tickets-agent/`.

On `main`, the `nat-streaming-react` distribution built from `agent/` (and the agent image)
carries the licence documents too. `agent/LICENSE`, `agent/LICENSES/Apache-2.0.txt`
and `agent/THIRD_PARTY_NOTICES.md` are byte-identical copies of the root files,
which stay authoritative, and are listed in `project.license-files`.
`make license-check` fails if a copy drifts. `make package-license-check` builds the
sdist, the wheel, a wheel from the sdist and an installed copy, and checks each for
the complete texts.
