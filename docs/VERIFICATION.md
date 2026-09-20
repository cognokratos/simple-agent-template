# Verification

What you can check, what it needs, and what it proves.

## Without Docker, a cluster or a model

```
make static-check
```

| Step | Proves |
| --- | --- |
| `npm run verify:nat-wire` | Scalar SSE chunks (`"100"`, `"true"`, dates) survive the wire contract |
| evaluator unit tests | Parser, scorers and provenance, 54 cases |
| `verify_security_config.py` | Resolved Compose topology: ports, network membership, credential agreement |
| `verify_security_sources.py` | Source wiring: gateway routes and properties, the patch scripts stay deleted, the guardrail event-name contract, no credential in `/version`, no workflow input interpolated into a shell command |

Needs only `python3`, `node` and the Docker CLI (for `docker compose config` —
no daemon interaction, no containers).

## With the Rust toolchain

```
cd gateway    && cargo test && cargo clippy --all-targets -- -D warnings
cd mcp-server && cargo test && cargo clippy --all-targets -- -D warnings
make verify-approvals-rust
```

53 gateway tests: cookies, CSRF, session generation discipline, PKCE, issuer
selection, message validation, identity-header encoding, routing and hardening.

25 MCP tests: approval signature, binding, lifetime, payload digest, the action
registry and every transition rule, plus the cross-language check that a
Python-minted token is accepted.

## With the cluster running

```
make dev          # build and start
make wait         # readiness
make health       # endpoints
make test         # everything below, in order
```

| Target | Needs | Proves |
| --- | --- | --- |
| `make verify-mcp` | cluster | MCP refuses a missing key and accepts the agent's |
| `make verify-input-guardrails` | cluster | Decision precedence, forged assistant history, strict boolean parsing |
| `make verify-output-guardrails` | cluster | Config invariants, secret patterns, real Presidio masking |
| `make verify-rails` | cluster | The real NeMo runtime: blocking, split credentials, reuse, concurrency |
| `make verify-trace-pipeline` | cluster | Trace context, bounds, redaction, error capture |
| `make verify-approvals` | cluster | The approval boundary, agent side |
| `make network-test` | cluster | Runtime east-west reachability |
| `make auth-test` | cluster | Every authentication boundary end to end |
| `make security-test` | cluster | The three above plus topology |

`verify-rails` replaces the rail LLM with a deterministic fake, so what is under
test is the rail wiring rather than the model. It needs no network.

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

`.github/workflows/ci.yml` runs the deterministic half on every push and pull
request: Python, both Rust crates with Clippy, the UI typecheck/contract/build,
and Compose topology. It reads no secret and works on a fork.

`.github/workflows/live-evaluation.yml` is manual, takes the endpoint per run,
and reads its key from a named environment that is absent by default.

## What is not verified

See [LIMITATIONS.md](LIMITATIONS.md).
