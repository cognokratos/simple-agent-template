#!/usr/bin/env python3
"""Static assertions for security-critical source wiring."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"security source check failed: {message}")


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def crate_text(path: str) -> str:
    """Every Rust source file of a crate, concatenated.

    These checks assert that a security control is *present in the crate*, not that
    it lives in a particular file. Pinning them to `src/main.rs` meant splitting a
    2,000-line module into named ones would have reported the MCP's API-key
    comparison, header stripping and token replay protection as missing.
    """
    files = sorted((ROOT / path).rglob("*.rs"))
    require(bool(files), f"no Rust sources found under {path}")
    return "\n".join(file.read_text(encoding="utf-8") for file in files)


def check_guardrail_event_names() -> None:
    """Every guardrail decision event the agent emits must be recognised by the evaluator.

    These are magic strings shared between two separately deployed codebases with
    no import path between them. The output name drifted once already — the agent
    emitted `guardrail_output_secret_regex_decision` while the evaluator matched a
    `..._regex_presidio_decision` name left from when Presidio was in the pipeline
    — and because nothing scored on it, the output rail simply went unmeasured
    without any test failing.
    """
    import re

    emitted = set(
        re.findall(
            r'_emit_nat_evaluation_event\(\s*"(guardrail_[a-z0-9_]+_decision)"',
            text("agent/src/nat_streaming_react/text_guardrails.py"),
        )
    )
    require(emitted, "no guardrail decision events found in text_guardrails.py")

    client = text("evaluation/client.py")
    declared = set(re.findall(r'^(?:INPUT|OUTPUT)_GUARDRAIL_EVENT = "([^"]+)"', client, re.MULTILINE))
    prefixes = set(re.findall(r'^(?:INPUT|OUTPUT)_GUARDRAIL_EVENT_PREFIX = "([^"]+)"', client, re.MULTILINE))
    require(prefixes, "evaluator declares no guardrail event prefixes")

    for name in sorted(emitted):
        require(
            name in declared or any(name.startswith(prefix) for prefix in prefixes),
            f"agent emits guardrail event {name!r} that evaluation/client.py does not recognise",
        )
    for name in sorted(declared):
        require(
            name in emitted,
            f"evaluation/client.py expects guardrail event {name!r} that the agent never emits",
        )


def main() -> None:
    check_guardrail_event_names()

    required_ui_routes = (
        "ui/app/api/gateway/auth/login/route.ts",
        "ui/app/api/gateway/auth/callback/route.ts",
        "ui/app/api/gateway/auth/session/route.ts",
        "ui/app/api/gateway/auth/logout/route.ts",
        "ui/app/api/gateway/chat/route.ts",
        "ui/app/api/gateway/interactions/[executionId]/[interactionId]/route.ts",
    )
    for route in required_ui_routes:
        require((ROOT / route).is_file(), f"missing UI route {route}")
    require(not (ROOT / "ui/app/api/auth").exists(), "legacy /api/auth routes still exist")
    require(not (ROOT / "ui/app/api/chat").exists(), "legacy /api/chat route still exists")

    page = text("ui/app/page.tsx")
    require(
        'new AssistantChatTransport({ api: "/api/gateway/chat" })' in page,
        "assistant-ui runtime is not pinned to /api/gateway/chat",
    )

    gateway = crate_text("gateway/src")
    for route in (
        '.route("/health", get(health))',
        '.route("/ready", get(ready))',
        '.route("/auth/login", get(auth::login))',
        '.route("/auth/callback", get(auth::callback))',
        '.route("/auth/session", get(auth::auth_session))',
        '.route("/auth/logout", post(auth::logout))',
        '.route("/api/chat", post(proxy::chat))',
        '"/api/interactions/{execution_id}/{interaction_id}"',
    ):
        require(route in gateway, f"missing fixed gateway route {route}")
    require('const BROWSER_COOKIE_PATH: &str = "/api/gateway"' in gateway, "gateway cookie path is not fixed to /api/gateway")
    require('const OIDC_CALLBACK_PATH: &str = "/api/gateway/auth/callback"' in gateway, "gateway callback path is not fixed to the UI callback")
    require("OIDC_CALLBACK_URL" in gateway, "gateway callback URL is not explicit")
    require("SameSite=Lax" in gateway and "SameSite=Strict" in gateway, "cookie SameSite flags missing")
    require("HttpOnly" in gateway, "session/login cookies are not HttpOnly")
    require("verify_csrf" in gateway, "CSRF enforcement is missing")
    require("Policy::none()" in gateway, "gateway HTTP redirects are not disabled")
    require("validate_interaction_response" in gateway, "HITL interaction response validation is missing")
    require("x-authenticated-user-id" in gateway and "x-request-id" in gateway, "trusted user identity/request headers are missing")
    # A session read, an await, and a write-back are three moments. A write-back
    # that does not check it is still updating the session it read re-inserted
    # sessions that logout had already removed, so logout stopped revoking.
    require(
        "replace_if_current" in gateway and "remove_if_current" in gateway,
        "gateway session write-backs are no longer generation-checked",
    )
    require(
        "WriteBack::Gone => Err(" in gateway,
        "a token refresh completing after logout can resurrect the session again",
    )
    # connect_timeout only covers TCP setup; without a request timeout a stalled
    # Keycloak hangs every authenticated request, since refresh sits inside auth.
    require(
        ".timeout(self.config.upstream_timeout)" in gateway,
        "Keycloak calls are no longer bounded by an upstream timeout",
    )
    # /auth/login needs no credentials, so refusing new logins at capacity is an
    # unauthenticated login outage. Pending logins must be evicted, not rejected.
    require(
        "too many pending login attempts" not in gateway,
        "the login flow refuses new logins at capacity again",
    )
    # Upstream error bodies name internal hosts and dependency versions.
    require(
        '"details"' not in gateway,
        "upstream error bodies are relayed to the browser again",
    )

    # NAT authenticates its callers from our own front-end worker, registered
    # through the supported `runner_class` extension point. It must never go back
    # to being a build-time rewrite of NAT's installed FastAPI worker.
    require(
        not (ROOT / "agent/patch_nat_api_key.py").exists(),
        "the NAT FastAPI source patch is back; use nat_streaming_react.fastapi_worker instead",
    )
    nat_worker = text("agent/src/nat_streaming_react/fastapi_worker.py")
    require("NAT_GATEWAY_API_KEY" in nat_worker, "NAT API-key environment variable missing")
    require("hmac.compare_digest" in nat_worker, "NAT key comparison is not constant time")
    require(
        'await self.app({**scope, "headers": sanitized}, receive, send)' in nat_worker,
        "NAT does not strip the service key from the request scope",
    )
    require(
        'app.middleware("http")' not in nat_worker and "BaseHTTPMiddleware)" not in nat_worker,
        "NAT auth must stay pure ASGI so it cannot buffer the streamed response",
    )
    require(
        'PUBLIC_PATHS: frozenset[str] = frozenset({"/health", "/health/live", "/health/ready"})' in nat_worker,
        "NAT unauthenticated path allow-list changed",
    )
    require(
        "runner_class: nat_streaming_react.fastapi_worker.AuthenticatedFastApiFrontEndPluginWorker"
        in text("agent/config.yml"),
        "NAT does not load the authenticated front-end worker",
    )

    # Observability is built on NAT's telemetry exporter/processor extension
    # points and standard OpenTelemetry context, never on a runtime source patch.
    require(
        not (ROOT / "agent/patch_nat_single_trace.py").exists(),
        "the NAT runner source patch is back; use nat_streaming_react.observability instead",
    )
    exporter = text("agent/src/nat_streaming_react/observability/mlflow_exporter.py")
    require("@register_telemetry_exporter" in exporter, "the telemetry exporter is not a registered NAT plugin")
    require("WorkflowContentProcessor" in exporter and "SensitiveHeaderRedactionProcessor" in exporter,
            "the exporter does not install the span normalization and redaction processors")
    require("_type: etf_research_otlp" in text("agent/config.yml"),
            "NAT does not use the application's telemetry exporter")
    trace_ctx = text("agent/src/nat_streaming_react/observability/trace_context.py")
    require("TraceContextTextMapPropagator" in trace_ctx, "W3C trace context propagation is missing")
    require("NonRecordingSpan" in trace_ctx, "the OpenTelemetry parent context bridge is missing")
    require("_root_span_id" in trace_ctx, "NAT's root span id is not pre-set, so Guardrails cannot join the trace")
    require("WorkflowTraceContextMiddleware" in text("agent/src/nat_streaming_react/fastapi_worker.py"),
            "the request trace context is not installed at the HTTP boundary")
    processor = text("agent/src/nat_streaming_react/observability/trace_processor.py")
    require("authorization" in processor and "cookie" in processor,
            "the telemetry redaction deny-list lost its credential headers")
    register_py = text("agent/src/nat_streaming_react/register.py")
    require("answer.add(text)" in register_py,
            "streamed text is no longer captured for observability")
    require(register_py.index("yield ChatResponseChunk.create_streaming_chunk(\n                            text,")
            < register_py.index("answer.add(text)"),
            "observability capture must happen after the chunk is yielded, never before")

    mcp = crate_text("mcp-server/src")
    require("MCP_API_KEY" in mcp, "MCP API-key environment variable missing")
    require("constant_time_eq" in mcp, "MCP key comparison is not constant time")
    require("request.headers_mut().remove(header::AUTHORIZATION)" in mcp, "MCP does not strip the service key")
    require("HITL_APPROVAL_SECRET" in mcp, "MCP does not require the HITL approval secret")
    require("consumed_approval_tokens" in mcp, "MCP does not enforce one-time approval tokens")
    require("MAX_APPROVAL_LIFETIME_SECONDS" in mcp, "MCP does not bound approval token lifetime itself")
    require("approvals.verify(" in mcp, "MCP mutation approval verification is missing")
    # Reading the row on the pool and then opening a transaction to write made
    # every state precondition racy: two approvals could both observe UNREVIEWED
    # and both commit. Each mutation must take the row lock instead.
    require("FOR UPDATE" in mcp, "MCP mutations no longer lock the ETF row they decide on")
    for mutation in ("commit_evaluation", "shortlist_etf", "assign_etf"):
        body = mcp.split(f"pub async fn {mutation}(", 1)[1].split("\n    #[tool", 1)[0]
        require(
            "store::lock_etf(&mut tx" in body,
            f"{mutation} reads the ETF without locking it for the transaction",
        )
    # The deterministic policy must be re-enforced at the moment of the write, not
    # only when the human was shown it. Every decision-bearing mutation calls the
    # one shared hard-constraint check.
    for mutation in ("commit_evaluation", "shortlist_etf"):
        body = mcp.split(f"pub async fn {mutation}(", 1)[1].split("\n    #[tool", 1)[0]
        require(
            "rules::blocking_hard_constraint(" in body,
            f"{mutation} no longer re-checks hard constraints before writing",
        )
    # A hard constraint marked non-bypassable must not be reachable by any actor,
    # including one holding a valid human approval.
    require(
        "!(hit.bypassable && override_applied)" in mcp,
        "a human override can now lift a non-bypassable hard constraint",
    )
    # The deterministic decision is the default. Both decision-bearing mutations
    # must derive it through the one shared reconciliation, which reads the
    # engine's result and treats a model recommendation as advisory in *both*
    # directions. Deriving it as `llm_recommendation.unwrap_or(rules_decision)`
    # made a conservative model recommendation the system decision, so a human
    # confirming the engine's own answer was recorded as overriding it.
    for mutation in ("commit_evaluation", "shortlist_etf"):
        body = mcp.split(f"pub async fn {mutation}(", 1)[1].split("\n    #[tool", 1)[0]
        require(
            "rules::reconcile_decision(" in body,
            f"{mutation} no longer reconciles the decision through the shared authority rule",
        )
    require(
        "let override_applied = requested_decision != rules_decision;" in mcp,
        "an override is no longer defined as differing from the deterministic decision",
    )
    require(
        "unwrap_or_else(|| evaluation.decision.clone())" not in mcp
        and "recommendation.clone().unwrap_or" not in mcp,
        "the default decision is derived from the model recommendation again",
    )
    # A search that orders or filters on a stored column reports a decision nobody
    # made on a fresh database, and a stale one after any policy change.
    store_rs = text("mcp-server/src/store.rs")
    search_sql = store_rs.split("pub async fn search_etfs(", 1)[1].split("\npub async fn", 1)[0]
    for stored in ("ORDER BY investment_score", "investment_score >=", "LOWER(decision)"):
        require(
            stored not in search_sql,
            f"search_etfs filters or orders on the stored column: {stored!r}",
        )
    # The committed columns are a human decision. Seeding them presents the whole
    # universe as decided by nobody.
    seed_sql = store_rs.split("pub async fn upsert_seed_etf(", 1)[1]
    for column in ("decision", "investment_score", "review_state"):
        require(
            f"{column}=EXCLUDED.{column}" not in seed_sql,
            f"the seeder writes the committed column {column!r}",
        )
    # An assignment creates no decision, so it must not write decision columns that
    # would be stamped with the current policy version while carrying an older
    # score. Both generations go into details.policy_generations instead.
    assign_body = mcp.split("pub async fn assign_etf(", 1)[1].split("\n    #[tool", 1)[0]
    audit_call = assign_body.split("store::write_audit(", 1)[1].split("\n        .await?;", 1)[0]
    for field in ("rules_decision:", "final_decision:", "investment_score:", "rules_version:"):
        require(
            field not in audit_call,
            f"the assignment audit event populates {field!r}, mixing two policy generations",
        )
    require(
        "policy_generations(&etf, &evaluation)" in audit_call,
        "the assignment audit event no longer records both policy generations separately",
    )
    # The whole point of the project: policy is data, and an inconsistent
    # specification must stop the service rather than silently rescore everything.
    require(
        "RulesSpec::parse(" in mcp and "spec.validate()?" in mcp,
        "the rules specification is no longer validated at startup",
    )

    approval = text("agent/src/nat_streaming_react/approval.py")
    require("prompt_user_input" in approval, "NAT native human interaction is missing")
    require("x-authenticated-user-id" in approval and "x-request-id" in approval, "approval token is not bound to trusted request identity")
    require("HITL_APPROVAL_SECRET" in approval, "NAT approval signer secret is missing")
    require("from __future__ import annotations" not in approval, "approval tool uses postponed annotations that break NAT runtime type inspection")
    for model in ("CommitEvaluationRequest", "ShortlistEtfRequest", "AssignEtfRequest"):
        require(f"request: {model}" in approval, f"approval function no longer exposes the typed {model} schema")
    # Per-action schemas are the point: a single multi-action tool had to accept a
    # union of fields and validate them by hand, which is what let the model omit
    # whichever field the chosen action required.
    require("assignee: str = Field(" in approval, "assignment no longer requires a research owner by type")
    require("research_note: str = Field(" in approval, "shortlisting no longer requires a research note by type")
    # The mutations stay registered and approval-gated on the MCP, but must not be
    # in the model's MCP toolset: the model should have no capability to change
    # state except through the approval-gated NAT functions above.
    mcp_include = text("agent/config.yml").split("include:", 1)[1].split("tool_call_timeout", 1)[0]
    for mutation in ("commit_evaluation", "shortlist_etf", "assign_etf"):
        require(
            f"- {mutation}" not in mcp_include,
            f"{mutation} is still exposed to the model as an MCP tool",
        )

    agent_config = text("agent/config.yml")
    require("custom_headers:" in agent_config, "NAT MCP custom headers are missing")
    require("Authorization: Bearer ${MCP_API_KEY}" in agent_config, "NAT does not send the MCP key")
    require("mask sensitive data on output" not in agent_config, "generic output PII masking is still enabled")
    require("sensitive_data_detection:" not in agent_config, "Presidio sensitive-data output configuration is still enabled")
    require("- regex check output" in agent_config, "deterministic output secret scanning is missing")
    text_guardrails = text("agent/src/nat_streaming_react/text_guardrails.py")
    require("stream_async" in text_guardrails, "NeMo output streaming was disabled")
    require("buffered_items" not in text_guardrails, "output rail still buffers the full response")
    # NeMo 0.21 resolves $bot_message into the flow config of the LLMRails running
    # the rail. Sharing one instance across concurrent streams let a credential in
    # one response be checked against the other's text and released in full;
    # verify_guardrails_rails.py measures both halves of that.
    require(
        "self.rails_pool.acquire()" in text_guardrails,
        "guardrail evaluation no longer leases an isolated rails instance",
    )
    require(
        "self._llm_rails.stream_async" not in text_guardrails
        and "self._llm_rails.generate_async" not in text_guardrails,
        "guardrail evaluation is back on the process-wide shared rails instance",
    )
    # The approval mutation is a synchronous socket read; NAT serves every request
    # on one event loop, so it must never run inline.
    approval_py = text("agent/src/nat_streaming_react/approval.py")
    require(
        "await asyncio.to_thread(_execute_blocking" in approval_py,
        "the approval mutation blocks the event loop again",
    )
    # The model sees the whole client-supplied conversation; the rail must not see
    # only the newest turn.
    require(
        "_prior_turn_text" in text_guardrails,
        "fabricated conversation history is no longer screened",
    )

    # The pinned NeMo Guardrails release declares the regex output action without a
    # blocking output mapping, without **kwargs, and mutates the shared flow config
    # while streaming. All three are corrected from our own code through supported
    # extension points; none of them may come back as a site-packages edit.
    require(
        not (ROOT / "agent/patch_nemoguardrails_regex.py").exists(),
        "the NeMo Guardrails source patch is back; use nat_streaming_react.guardrails_compat instead",
    )
    guardrails_compat = text("agent/src/nat_streaming_react/guardrails_compat.py")
    require(
        "rails.register_action(detect_regex_pattern, REGEX_ACTION_NAME)" in guardrails_compat,
        "the blocking regex action is not registered through LLMRails.register_action",
    )
    require(
        "output_mapping=regex_blocked_mapping" in guardrails_compat,
        "the regex compatibility action declares no blocking output mapping",
    )
    require(
        "register_regex_rail_compatibility(self._llm_rails)" in text_guardrails,
        "the middleware does not register the blocking regex action",
    )
    # The restore moved into RailsPool.acquire(), which is now the single place a
    # rail evaluation begins — so it happens before every invocation by
    # construction rather than by two call sites remembering to do it.
    guardrails_compat = text("agent/src/nat_streaming_react/guardrails_compat.py")
    require(
        "entry[1].restore()" in guardrails_compat,
        "rail flow parameters are not restored before every rail invocation",
    )

    compose = text("docker-compose.yml")
    require("mcp-inspector:" in compose, "MCP Inspector service is missing")
    require("127.0.0.1}:6274:6274" in compose, "MCP Inspector is not loopback-bound")

    # The Inspector is built from the official upstream image rather than pulled
    # directly, because Inspector 2.x needs a reachable Secret Service before
    # `GET /api/servers` will succeed. The wrapper must stay a thin, pinned layer
    # over the official image: no forked or vendored Inspector, no floating tag.
    require(
        "context: ./mcp-server/inspector" in compose,
        "MCP Inspector does not build from the audited ./mcp-server/inspector context",
    )
    inspector_dockerfile = text("mcp-server/inspector/Dockerfile")
    require(
        "FROM ghcr.io/modelcontextprotocol/inspector:2.1.0" in inspector_dockerfile,
        "MCP Inspector is not built from the pinned official image "
        "ghcr.io/modelcontextprotocol/inspector:2.1.0",
    )
    require(
        ":latest" not in inspector_dockerfile,
        "MCP Inspector base image must be version-pinned, not a floating tag",
    )
    require(
        "USER node" in inspector_dockerfile,
        "MCP Inspector must drop back to the unprivileged node user",
    )

    # Server credentials are rendered at start-up from the environment. The
    # template must therefore carry a placeholder, never a literal key, so a real
    # MCP_API_KEY can never be committed to the repository through this path.
    inspector_template = text("mcp-server/inspector/mcp.template.json")
    require(
        "${MCP_API_KEY}" in inspector_template,
        "MCP Inspector config template does not take the MCP key from the environment",
    )
    require(
        "dev-mcp-api-key" not in inspector_template,
        "MCP Inspector config template hardcodes an API key instead of a placeholder",
    )
    require(
        "http://mcp-server:8080/mcp" in inspector_template,
        "MCP Inspector is not wired directly to the MCP service",
    )
    require(
        "mcp.template.json:/config/mcp.template.json:ro" in compose,
        "MCP Inspector config template is not mounted read-only",
    )
    require(
        "MCP_API_KEY:" in compose.split("mcp-inspector:", 1)[1].split("mlflow:", 1)[0],
        "MCP Inspector service does not receive MCP_API_KEY to render its config",
    )

    inspector_entrypoint = text("mcp-server/inspector/entrypoint.sh")
    require(
        "envsubst" in inspector_entrypoint,
        "MCP Inspector entrypoint does not render its config from the template",
    )
    require(
        "MCP_INSPECTOR_API_TOKEN" in compose,
        "MCP Inspector does not keep its own API-token authentication",
    )

    evaluator = text("evaluation/client.py")
    require("AGENT_API_KEY" in evaluator, "evaluator does not require the NAT key")

    print("Security-critical source wiring passed.")


if __name__ == "__main__":
    main()
