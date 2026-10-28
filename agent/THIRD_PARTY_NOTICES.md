# Third-party notices and licence exceptions

The [`LICENSE`](LICENSE) file (MIT, Copyright (c) 2026 Victor Nitu) covers the
original code and documentation in this repository. The material below is not
covered by it, or not only by it. Its own terms continue to apply.

The authoritative copies of this file, `LICENSE` and `LICENSES/Apache-2.0.txt`
are at the repository root. Byte-identical copies in `agent/` are copied into
the agent's Docker image (`/usr/share/doc/tickets-agent/`), because the image
build sees only that directory; `scripts/verify_agent_package_licenses.py`
checks that they match, and with `--image` that the built image carries them.
Paths below are relative to the repository root.

## The agent

The agent in `agent/src/` is a Rust service built on Rig, written for this
repository. It replaced an earlier implementation on NVIDIA NeMo Agent Toolkit
(NAT), preserved at the tag
[`nat-1.9-final`](https://github.com/cognokratos/simple-agent-template/tree/nat-1.9-final)
and the branch `legacy/nat`. **None of the Apache-2.0 files derived from NeMo
Agent Toolkit exist in the current tree**: `agent/src/nat_streaming_react/register.py`,
`text_guardrails.py` and `observability/otlp_exporter.py` were removed with the
rest of the Python agent. They, their NVIDIA notices and their provenance
record remain at that tag; consult its
[`THIRD_PARTY_NOTICES.md`](https://github.com/cognokratos/simple-agent-template/blob/nat-1.9-final/THIRD_PARTY_NOTICES.md)
for them.

The Rust agent reproduces the *behaviour* of the NAT agent — the same contract,
policies, prompts and token format — but contains no code copied from NeMo Agent
Toolkit, NeMo Guardrails or Presidio:

* the system prompt, self-check prompt, tool descriptions and output patterns
  in `agent/config.yml` are this repository's own MIT-licensed configuration,
  carried over from the NAT agent's `agent/config.yml`;
* the input classifier's verdict parsing reimplements the documented behaviour
  of NeMo Guardrails 0.21's `is_content_safe` parser, and the PII recognisers
  are independent deterministic implementations of the same entity list, not
  a port of Presidio.

`agent/src/approval/token/against_the_mcp_verifier/mod.rs` and
`agent/tests/support/mod.rs` compile `mcp-server/src/approval.rs` (this
repository, MIT) into tests; nothing is copied.

`LICENSES/Apache-2.0.txt` is kept: many Rust crates the agent links are
licensed `MIT OR Apache-2.0` or `Apache-2.0`, and the image ships the text.

## Dependencies

Dependencies installed at build or run time are not part of this repository.
Each is used under its own licence. For the agent that means
the Rust crates locked in `agent/Cargo.lock` — at the time of writing 298 in the
runtime dependency closure, all under permissive licences (MIT, Apache-2.0,
BSD-2/3-Clause, ISC, Zlib, Unicode-3.0, Unlicense, CC0-1.0, BSL-1.0), with one
data licence:

* `webpki-roots` / `webpki-root-certs` — the Mozilla CA certificate bundle,
  under **CDLA-Permissive-2.0**, which asks that its text accompany the data
  when it is shared. The bundle is compiled into the agent binary (as it is
  into the gateway's). The licence text is at
  <https://cdla.dev/permissive-2-0/>.

Reproduce the inventory with `cargo metadata --format-version 1 --locked` in
`agent/`. Keycloak, PostgreSQL, the OpenTelemetry Collector, assistant-ui and
the gateway's and MCP server's crates are used under their own licences.

### Observability and evaluation services

None of these is copied into this repository or into the agent image. Docker
Compose pulls each from its upstream registry at the pinned version, under its
own licence:

* **Arize Phoenix** (`arizephoenix/phoenix:version-20.26.1-nonroot`, default
  stack) — **Elastic License 2.0 (ELv2)**, a source-available licence rather
  than an OSI-approved open-source one. It permits use, modification and
  redistribution, but **not providing Phoenix to third parties as a hosted or
  managed service**, nor circumventing its licence-key functionality. Running
  it as this template does — a local developer workbench, or an internal
  deployment for your own team — is the use it is licensed for; offering it as
  a service to others is not. Text:
  <https://github.com/Arize-ai/phoenix/blob/main/LICENSE>.
* **MLflow** (`ghcr.io/mlflow/mlflow:v3.14.0`, optional `mlflow` profile) —
  Apache-2.0.
* **PyYAML** 6.0.3 (installed into the evaluator image from
  `evaluation/requirements.txt`) — MIT. The evaluator otherwise uses only the
  Python standard library; it installs no Phoenix, OpenInference or
  OpenTelemetry package.
