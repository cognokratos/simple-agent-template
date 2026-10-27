# Verification

What you can check, what it needs, and what it proves.

`make help` lists every target; [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)
is the authoritative list of what gates a pull request.

## Without Docker, a cluster or a model

```
make static-check
```

| Step | Proves |
| --- | --- |
| `make verify-stream-adapter` (`ui/scripts/verify-agent-wire.mjs`) | Scalar SSE chunks (`"100"`, `"true"`, dates) survive the wire contract |
| `make eval-test-host` | Evaluator parser, scorers and provenance (`evaluation/tests/`) |
| `verify_security_config.py` (in `make security-config-test`) | Resolved Compose topology: ports, network membership, credential agreement |
| `verify_security_sources.py` (in `make security-config-test`) | Source wiring: gateway routes and properties; the retired NAT agent's files stay deleted; the agent requires its service key and an asserted identity, compares the key in constant time and strips it before handlers; Rig is pinned exactly; tool policy runs at the dispatch hook and is re-checked by executors; the shipped configuration exposes no mutation tool; the guardrail event-name contract with the evaluator; every gateway identity header has a telemetry decision; no credential or prompt text in `/version`; the OTLP export filter is fixed in code; no workflow input interpolated into a shell command, and `ci.yml` reads no secret |
| `make docs-check` (`verify_docs.py`) | Every relative Markdown link and heading anchor resolves, and every `make <target>` named in the docs exists. `verify_docs_test.py` plants broken references to prove the checker fails on them. |
| `make license-check` | The licence copies in `agent/` match the repository-root originals |

Needs only `python3`, `node` and the Docker CLI (for `docker compose config` —
no daemon interaction, no containers).

`docs-check` verifies references, not content. It does not check that the docs
are semantically correct, that runtime or model-dependent claims still hold, or
that external URLs resolve.

## With the Rust toolchain

The agent's checks need only a Rust toolchain (≥ 1.95), no cluster and no
model. The integration suites drive the real service against a fake
OpenAI-compatible endpoint and an in-process MCP server.

```
make agent-check     # cargo fmt --check, cargo clippy --locked -D warnings, cargo test --locked
make agent-test      # the tests alone
```

| Suite | Proves |
| --- | --- |
| unit tests (`agent/src`) | config fail-fast, input precedence and verdict parsing (matching NeMo Guardrails 0.21's documented parser), strict schemas, tool policy, the streaming window across every chunk split, PII recognisers on the repository's fixtures, the state machine, the interaction registry, token minting |
| `against_the_mcp_verifier` | tokens minted by the agent verify against `mcp-server/src/approval.rs` itself, compiled into the test |
| [`tests/contract.rs`](../agent/tests/contract.rs) | the [agent-service contract](AGENT-SERVICE-CONTRACT.md) over HTTP |
| [`tests/agent_loop.rs`](../agent/tests/agent_loop.rs) | a scripted adversarial model: unknown tools, invented privileged arguments, forged identity, split secrets, endless tool calls, provider and MCP errors, disconnect, and the input rail end to end |
| [`tests/approvals.rs`](../agent/tests/approvals.rs) | the whole approval flow and its attacks through the real service |
| [`tests/observability.rs`](../agent/tests/observability.rs) | one trace per request with Rig's spans, `traceparent`, no credential, identity or token in any span |

The `verify-*` targets run named subsets of the same suites on the host:

| Target | Runs | Proves |
| --- | --- | --- |
| `make verify-llm-config` | `config::` unit tests | Configuration parsing fails fast; an empty optional parameter is omitted and a set one (`none` included) is sent verbatim. Each case pins its own input, so the result does not depend on the caller's `.env` |
| `make verify-input-guardrails` | `guardrails::input` and `config::` | Decision precedence, forged assistant history, strict verdict parsing, the per-layer switches |
| `make verify-output-guardrails` | `guardrails::output` and `guardrails::pii` | Progressive release, cross-chunk secret blocking, PII masking |
| `make verify-rails` | `tests/agent_loop.rs` | Both rails through the real service with a scripted model, so what is under test is the wiring rather than a model. It needs no network |
| `make verify-guardrails` | the three above | |
| `make verify-approvals` | `approval::` and `tests/approvals.rs` | The approval boundary, agent side |
| `make verify-trace-pipeline` | `tests/observability.rs` | Trace context, redaction (in both `OTEL_TRACE_USER_ID` modes), released rather than raw text on a block |

The other two crates:

```
cd gateway    && cargo clippy --locked --all-targets --all-features -- -D warnings && cargo test --locked
cd mcp-server && cargo clippy --locked --all-targets --all-features -- -D warnings && cargo test --locked
make verify-approvals-rust   # the MCP approval verifier and mutation-policy subsets
```

The gateway test suite covers cookies, CSRF, session generation discipline, PKCE, issuer
selection, message validation, identity-header encoding, routing and hardening.

The MCP test suite covers approval signature, binding, lifetime, payload digest, the action
registry and every transition rule, plus a fixed token from the retired Python
minter, which proves the token format did not change when the agent was
rewritten.

## With the cluster running

```
make dev          # build and start
make wait         # readiness
make health       # endpoints
make test         # eval-test, agent-test, verify-approvals-rust, security-test
```

| Target | Needs | Proves |
| --- | --- | --- |
| `make verify-mcp` | cluster | MCP refuses a missing key and accepts the agent's (probed from the agent container) |
| `make network-test` | cluster | Runtime east-west reachability |
| `make auth-test` | cluster | Every authentication boundary end to end |
| `make security-test` | cluster | The three above plus `security-config-test` |
| `make eval-test` | Docker | The evaluator unit tests inside the evaluator image |

The agent image is distroless, so in-container checks use the binary's `probe`
subcommand rather than a shell (`make shell-agent` prints how).

## With a model available

```
make trace-test              # real requests, asserted against MLflow
make traces                  # print recent span trees
make version                 # what the running agent reports about itself
make eval SUITE=grounding    # one evaluation suite
make eval-all                # all four, with gates
```

These are the only checks that need an LLM. They are non-deterministic and are
deliberately not part of the pull-request gate — see
[EVALUATION.md](EVALUATION.md).

## Manual scenarios

[TEST-SCENARIOS.md](TEST-SCENARIOS.md) lists prompts to type into the UI and what
should happen, covering tool calls, guardrail blocks and allows, output masking
and trace shape.

## CI

`.github/workflows/ci.yml` runs the deterministic half on every push to `main`
and every pull request: the Python checks (evaluator tests, source wiring,
documentation, licence copies, evaluation-artifact gate), all three Rust crates
(`agent`, `gateway`, `mcp-server`: Clippy with warnings denied, tests, a
current lockfile, and `cargo fmt --check` for the agent), the UI
typecheck/wire contract/build, and the Compose topology. It reads no secret and
works on a fork.

`.github/workflows/live-evaluation.yml` is manual, takes the endpoint per run,
and reads its key from a named environment that is absent by default.

## What is not verified

See [LIMITATIONS.md](LIMITATIONS.md).
