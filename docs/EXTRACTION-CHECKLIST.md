# Extraction checklist

What was taken from `example/etf-research-agent` into this template, where it
landed, and what was deliberately left behind. Kept so omissions are reviewable.

Source ref: `origin/example/etf-research-agent` @ `7f4c329`, one commit ahead of
`a0667e6`, 136 changed files.

Status: **done** · **adapted** (taken, but changed materially) · **deferred**
(with a reason) · **rejected** (with a reason)

## Streaming and UI

| Item | Destination | Status |
| --- | --- | --- |
| Scalar SSE parsing fix | `ui/lib/nat-wire.ts`, chat route | done |
| Equivalent fix in the evaluator | `evaluation/client.py` | adapted — also fixed two further defects the source has: `_normalize` coerced nested `content` so structured chunks were dropped, and the MCP `{type,text}` unwrap never fired |
| Regression tests, both paths | `ui/scripts/verify-nat-wire.mjs`, `ScalarWireTests` | done |
| `react-markdown` + GFM, no raw HTML | `ui/app/page.tsx`, `globals.css` | done |
| Responsive/prose styles | `globals.css` | done |
| ETF welcome prompts, disclaimers, labels | — | rejected — branding |

## Gateway

| Item | Destination | Status |
| --- | --- | --- |
| Nine-module split | `gateway/src/*.rs` | done |
| Generation-checked session write-backs | `session.rs` | done |
| Explicit non-streaming timeouts | `oidc.rs`, `config.rs` | done |
| JWKS cache + bounded forced refetch | `oidc.rs` | done |
| Bounded session and pending-login stores | `session.rs`, `auth.rs` | done |
| Per-session concurrent-stream limit | `proxy.rs` | done |
| Strict security-env parsing | `config.rs` | done |
| Safe same-origin redirects incl. relative | `auth.rs` | done |
| Non-ASCII identity headers | `proxy.rs` | done |
| Sanitized client-facing upstream errors | `error.rs` | done |
| 53 unit and routing tests | throughout | done |
| `Cargo.lock` | `gateway/Cargo.lock` | done |
| Roles re-read on refresh | `auth.rs` | done |
| ETF cookie/realm/crate names | — | rejected — branding |

## NAT authentication and compatibility

| Item | Destination | Status |
| --- | --- | --- |
| Custom worker via `runner_class` | `fastapi_worker.py`, `config.yml` | done |
| Pure-ASGI service-key auth, constant time | `fastapi_worker.py` | done |
| Credential stripped from the ASGI scope | `fastapi_worker.py` | done |
| Minimal unauthenticated health paths | `fastapi_worker.py` | done |
| Guardrails compatibility registration | `guardrails_compat.py` | adapted — **added** a `mask_sensitive_data` wrapper the source omits, because Presidio is retained here |
| Delete `patch_*.py` | — | done, after replacement coverage |
| Document private-NAT reliance | `observability/__init__.py`, `docs/OBSERVABILITY.md` | done — the source claims a supported-extension-point implementation without listing them |

## Guardrails

| Item | Destination | Status |
| --- | --- | --- |
| Blocking regex action registration | `guardrails_compat.py` | done |
| Flow-parameter mutation guard | `guardrails_compat.py` | done |
| Pooled isolated rail instances | `guardrails_compat.py` | done |
| Deterministic client-history checks | `text_guardrails.py` | done |
| Recovery after a blocked turn | `text_guardrails.py` | done |
| Exact rail-block envelope matching | `text_guardrails.py` | done |
| Safe boolean parsing | `text_guardrails.py` | done |
| Sequential + concurrent regression cover | `verify_guardrails_rails.py` | adapted — the source asserts the concurrency *symptom*, which is a race and flaky; this asserts the deterministic root cause and reports the symptom |
| Presidio removal | — | **rejected** — PII protection retained and made configurable; masking behaviour measured and documented. Streaming responses are masked too: since NeMo's streaming rail runner cannot rewrite text, the middleware buffers the complete answer and masks it once (`TextGuardrailsMiddleware._stream_with_buffered_masking`) rather than shipping the limitation undocumented or unfixed |
| ETF allow patterns and prompt policy | — | rejected — domain |

## Observability

| Item | Destination | Status |
| --- | --- | --- |
| Request trace context | `observability/trace_context.py` | done |
| Unified NAT + Guardrails traces | same | done |
| Question/answer capture, bounded | `observability/trace_content.py` | done |
| Truncation metadata | `trace_content.py`, `trace_processor.py` | done |
| Error and partial-stream capture | `trace_processor.py`, `text_guardrails.py`, `register.py` | adapted — a partial answer is kept *and* the error recorded, rather than the error replacing it; the answer is captured where the guardrail middleware actually releases text, not where the workflow function produces it, so a masked or blocked response cannot leak into the trace |
| Credential-header redaction | `trace_processor.py` | done |
| Registered OTLP exporter | `observability/otlp_exporter.py` | adapted — renamed from `etf_research_otlp` to `agent_otlp`; private `_span_prefix` read defensively |
| Trace inspection and e2e tools | `scripts/inspect_mlflow_traces.py`, `verify_traces_e2e.py` | done |
| Configurable content capture | `NAT_TRACE_CAPTURE_CONTENT` | adapted — the source captures unconditionally |

## Model configuration and provenance

| Item | Destination | Status |
| --- | --- | --- |
| `LLM_BASE_URL/API_KEY/MODEL`, guard model | `config.yml`, compose, `.env.example` | done |
| Empty reasoning settings omitted | `llm_config.py`, `text_guardrails.py` | adapted — the source's `${VAR:-null}` sends the literal string `"null"`; measured and fixed |
| Ollama-only `pull-models` | `Makefile` | done |
| Authenticated `/version` provenance | `provenance.py`, `fastapi_worker.py` | adapted — tools read from all function groups, not one hardcoded name |
| No credentials through `/version` | asserted in `verify_security_sources.py` | done |

## Evaluation

| Item | Destination | Status |
| --- | --- | --- |
| Improved SSE/event parsing | `client.py` | done |
| Tool-result capture and dedup | `client.py` | done |
| Unexpected human-interaction handling | `client.py` | done |
| Latency distributions | `runner.py` | done |
| JSON summaries with provenance | `runner.py` | done |
| Deployed-vs-harness consistency | `provenance.py` | done |
| MLflow prompt registry linkage | `provenance.py` | done |
| Parser/scorer/provenance tests | `evaluation/tests/` | done |
| Grounding methodology | `scorers.grounding_scores` | adapted — generic; dataset supplies vocabulary. Fixed a digit-run false positive that would have made the gate permanently red |
| Unsupported-action-claim detection | `scorers.action_claims` | adapted — domain-neutral verbs |
| Guardrail FP/FN separation | existing `guardrail_policy_scores` | preserved |
| Prompt-injection resistance | `scorers.injection_resistance_scores`, `datasets/injection.json` | adapted |
| Source-poisoning fixtures | `db/injection_test_fixtures.sql` | adapted — **seeded dedicated rows** instead of the source's poison/restore script, so nothing is ever mutated and there is nothing to restore |
| Existing tool-calling and guardrail suites | `datasets/` | preserved |
| ETF scorers, score expectations, vocabulary | — | rejected — domain |
| Checked-in `evaluation/results/*.json` | — | rejected — gitignored, so a CI artifact cannot be confused with a stale committed one |

## Human-in-the-loop approvals

| Item | Destination | Status |
| --- | --- | --- |
| NAT pause/resume protocol | `approval.py` | done |
| Choices, rationale text, cancellation | `approval.py`, UI card | done |
| UI approval cards with states | `ui/app/page.tsx` | done |
| Authenticated CSRF-protected proxying | gateway `proxy.rs`, UI route | done |
| Signed payloads, expiry, server ceiling | `approval.py`, `mcp-server/src/approval.rs` | done |
| Action/resource/request binding | `approval.rs` | done |
| Tamper and replay prevention | `approval.rs`, `mutation.rs` | done |
| Exact approved payload, no further model turn | `approval.py` | done |
| Transactional mutation + nonce + audit | `mutation.rs`, `db/init.sql` | done |
| `etf_id` → `resource_id` | throughout | done |
| ETF actions/decisions → app-defined | `mutation::ACTIONS` | done |
| Research notes → app-owned payload | signed `payload` object | done |
| Validate choices against the pending interaction | `interaction_guard.py` | **added** — the source validates against a hardcoded gateway list; that cannot generalize, and NAT itself performs no check at all |
| Verify the responder owns the execution | `interaction_guard.py` | **added** — not in the source; stock NAT treats two UUIDs as authorization |
| Opt-in demo, default read-only | `config.yml`, compose, CI assertion | done |
| Boundary tests | `verify_approval_tokens.py`, Rust tests | done |
| ETF rules engine, scores, investor profile | — | rejected — domain |

## Deployment and tooling

| Item | Destination | Status |
| --- | --- | --- |
| Segmented Compose networks | `docker-compose.yml` | done |
| Resolved-configuration topology checks | `scripts/verify_security_config.py` | adapted — extended with an explicit network matrix; also fixed the target, which had never passed |
| Runtime reachability tests | `make network-test` | done |
| MCP Inspector, authenticated, loopback | `mcp-server/inspector/`, `dev` profile | adapted — put behind an opt-in profile |
| Configurable MLflow host port | `MLFLOW_PORT` | done |
| Health/trace/verification/provenance targets | `Makefile` | done |
| Ignore-file improvements | `.gitignore` | done |
| Hardcoded `etf-research-agent` project name | — | rejected — pinned but overridable, so clones coexist |

## Builds and CI

| Item | Destination | Status |
| --- | --- | --- |
| UI lockfile + `npm ci` | `ui/package-lock.json`, `ui/Dockerfile` | done |
| Pinned assistant-ui versions | `ui/package.json` | done |
| Gateway `Cargo.lock` | `gateway/Cargo.lock` | done |
| Frontend typecheck + streaming contract | CI, `npm run` scripts | done |
| Deterministic CI | `.github/workflows/ci.yml` | adapted to this template's suites |
| Manual live-evaluation workflow | `.github/workflows/live-evaluation.yml` | adapted — see below |
| Free-text inputs in shell commands | — | **fixed** — the source is injectable; now passed via `env:` and enforced by a source check |
| Stale checked-in results uploaded as artifacts | — | **fixed** — gitignored, cleared, freshness-checked, credential-scanned |
| ETF deterministic-baseline CI step | — | rejected — domain |

## Documentation

| Item | Destination | Status |
| --- | --- | --- |
| Consolidated architecture/security/verification docs | `docs/*.md` | adapted |
| Known limitations and untested behaviour | `docs/LIMITATIONS.md` | done |
| How a domain application builds on this | `docs/EXTENDING.md` | done |
| Policy/state/audit reusable patterns | `docs/EXTENDING.md` | adapted — interfaces and the template's concrete implementation, not a policy framework |
| MIT root `LICENSE` | — | **rejected** — the repository declares Apache-2.0 in `gateway/Cargo.toml` and in Python SPDX headers, which the source leaves in place while adding a conflicting MIT file. Reported in `docs/LIMITATIONS.md#licensing` rather than resolved unilaterally |
| ETF datasets, schema, tools, prompts, diagrams | — | rejected — domain |

## Deferred

| Item | Reason |
| --- | --- |
| `docs/img/*.png` and `scripts/extract_diagrams.py` | The diagrams are ETF architecture renders. The extraction script is reusable but has nothing to render here; the documentation uses fenced ASCII, which diffs |
| `docs/DEMO.md`, `docs/ACCEPTANCE.md`, `docs/EVALUATION_ANALYSIS.md` | Walkthroughs and result analysis of the ETF application's own runs |
| `mcp-server/src/{domain,rules,store,seed,fixtures,server}.rs` | The ETF domain model and rules engine |
| `data/*.json` | ETF universe, investor profile, rules spec |
| `verify_hitl_override.py` | Drives an ETF-specific decision promotion end to end; the generic boundary is covered by `verify_approval_tokens.py` and the Rust suite |
