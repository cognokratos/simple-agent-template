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

**A fabricated prior *user* turn is not screened.** The input rail screens the
latest turn and any client-supplied *assistant* turn. It does not re-screen prior
user turns, because doing so made one refusal poison the rest of a conversation.
The same caller can send that text as the latest turn, where the full rail does
screen it. See [GUARDRAILS.md](GUARDRAILS.md).

**PII masking does not apply to streamed text.** On the streaming path NeMo uses
an action's result only to decide blocked/not-blocked. Credential protection is
the regex rail's job; masking takes effect on the non-streaming path. The
configured `score_threshold` is also not honoured by the pinned release's masking
action — the effective floor is Guardrails' hardcoded 0.4. Both are asserted by
`verify_output_guardrails.py` so they cannot drift unnoticed.

**Header redaction is not content redaction.** The telemetry processor removes
credential-bearing headers. A secret inside a tool result or a model answer is
not reached by it. See [OBSERVABILITY.md](OBSERVABILITY.md).

**Sessions are in memory.** One gateway instance, and a restart logs everyone
out.

**An interaction with no recorded owner is allowed through** unless
`HITL_STRICT_INTERACTION_OWNERSHIP=true`, so NAT's own OAuth consent flow keeps
working. Every interaction the approval module creates *is* recorded.

## Resource requirements

`make verify-output-guardrails` loads Presidio's analyzer, which pulls spaCy's
`en_core_web_lg` into memory — roughly 600 MB on top of the agent's own
footprint. On a Docker VM already near capacity the kernel kills it, which
surfaces as a bare `exit 137` rather than a failing assertion. The script warns
before that point. Give Docker headroom, or run the same script on the host
where the dependencies are installed.

Observed: on a 7.7 GB Docker VM with MLflow at 2 GB and an unrelated stack
running, the masking half was OOM-killed while the configuration, pattern and
wiring halves passed. The same script passed in full on the host.

## Dependency constraints

`nvidia-nat-security[guardrails]==1.8.0` pins `nemoguardrails>=0.11,<0.22`, so
0.23.0 — which fixes three streaming rail defects — cannot be installed.
`guardrails_compat.py` works around them from application code and self-disables
once the installed release is correct. Delete it when the pin allows `>=0.23`.

The observability package relies on three private NAT attributes, each listed
with its removal condition in `observability/__init__.py` and
[OBSERVABILITY.md](OBSERVABILITY.md). This is **not** a purely public-API
implementation.

`nvidia-nat[langchain]` pulls the full NAT LangChain dependency set, because
NAT 1.8's supported `react_agent` lives there and exposes no OpenAI-only extra.

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

The repository declares **Apache-2.0** in `gateway/Cargo.toml` and in the SPDX
headers of the Python sources under `agent/src/`. There is no root `LICENSE`
file. Anyone forking this should add one that matches those declarations, or
change the declarations deliberately — not both at once, and not silently.
