#!/usr/bin/env python3
"""Static assertions for security-critical source wiring."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"security source check failed: {message}")


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def main() -> None:
    required_ui_routes = (
        "ui/app/api/gateway/auth/login/route.ts",
        "ui/app/api/gateway/auth/callback/route.ts",
        "ui/app/api/gateway/auth/session/route.ts",
        "ui/app/api/gateway/auth/logout/route.ts",
        "ui/app/api/gateway/chat/route.ts",
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

    # The gateway is nine modules, so each assertion names the module that owns
    # the property. A property that moves must move deliberately, not silently.
    routes = text("gateway/src/http.rs")
    for route in (
        '.route("/health", get(health))',
        '.route("/ready", get(ready))',
        '.route("/auth/login", get(auth::login))',
        '.route("/auth/callback", get(auth::callback))',
        '.route("/auth/session", get(auth::auth_session))',
        '.route("/auth/logout", post(auth::logout))',
        '.route("/api/chat", post(proxy::chat))',
    ):
        require(route in routes, f"missing fixed gateway route {route}")
    require(".fallback(not_found)" in routes, "gateway does not reject unknown paths explicitly")
    require("security_headers" in routes, "gateway response hardening layer is missing")

    gateway_config = text("gateway/src/config.rs")
    require(
        'pub const BROWSER_COOKIE_PATH: &str = "/api/gateway"' in gateway_config,
        "gateway cookie path is not fixed to /api/gateway",
    )
    require(
        'pub const OIDC_CALLBACK_PATH: &str = "/api/gateway/auth/callback"' in gateway_config,
        "gateway callback path is not fixed to the UI callback",
    )
    require("OIDC_CALLBACK_URL" in gateway_config, "gateway callback URL is not explicit")
    # An unparseable security flag must keep its declared default rather than
    # resolving toward the weaker setting.
    require("pub fn parse_bool" in gateway_config, "gateway boolean parsing is not strict")

    cookies = text("gateway/src/cookies.rs")
    require(
        "SameSite=Lax" in cookies and "SameSite=Strict" in cookies,
        "cookie SameSite flags missing",
    )
    require("HttpOnly" in cookies, "session/login cookies are not HttpOnly")

    session = text("gateway/src/session.rs")
    require("pub fn verify_csrf" in session, "CSRF enforcement is missing")
    require("constant_time_eq" in session, "gateway token comparison is not constant time")
    # A write-back that does not quote the generation it read is what let an
    # in-flight refresh resurrect a logged-out session.
    require("replace_if_current" in session, "session write-backs are not generation-checked")
    require("remove_if_current" in session, "session revocation is not generation-checked")

    proxy = text("gateway/src/proxy.rs")
    require("stream_slots" in proxy, "per-session concurrent stream limit is missing")
    require("fn header_safe" in proxy, "identity headers can still be silently dropped")
    require(
        "x-authenticated-user-id" in proxy,
        "gateway does not inject the trusted identity header",
    )

    oidc = text("gateway/src/oidc.rs")
    require("JWKS_CACHE_TTL" in oidc, "JWKS responses are not cached")
    require("JWKS_REFETCH_FLOOR" in oidc, "forced JWKS refetch is not rate limited")
    require("validate_id_token" in oidc, "ID token validation is missing")
    require("code_challenge_method" in oidc, "PKCE is not sent to the authorization endpoint")
    require(
        "self.config.upstream_timeout" in oidc,
        "non-streaming upstream calls have no explicit timeout",
    )

    require("Policy::none()" in text("gateway/src/main.rs"), "gateway HTTP redirects are not disabled")

    # rust-agent branch: the agent is the Rig-based Rust service in agent/. The
    # properties asserted against NAT's front-end worker on `main` are asserted
    # here against the Rust modules that own them. A property that moves must
    # move deliberately, not silently.
    for removed in (
        "agent/src/nat_streaming_react",
        "agent/pyproject.toml",
        "agent/requirements.txt",
    ):
        require(
            not (ROOT / removed).exists(),
            f"{removed} is back; the NAT agent belongs to `main`, not this branch",
        )

    agent_settings = text("agent/src/config.rs")
    require('"NAT_GATEWAY_API_KEY"' in agent_settings, "agent service-key environment variable missing")
    require('"MCP_API_KEY"' in agent_settings, "agent MCP-key environment variable missing")
    require(
        "pub fn parse_bool" in agent_settings and "is not a recognised boolean" in agent_settings,
        "agent boolean parsing is not strict",
    )

    agent_auth = text("agent/src/api/auth.rs")
    require(
        "request.headers_mut().remove(header::AUTHORIZATION)" in agent_auth,
        "the agent does not strip the service key before handlers see it",
    )
    require("ct_eq" in agent_auth, "agent key comparison is not constant time")
    require(
        'pub const PUBLIC_PATHS: [&str; 3] = ["/health", "/health/live", "/health/ready"];' in agent_auth,
        "the unauthenticated agent surface is no longer liveness-only",
    )
    # Two questions, two layers: "is this the gateway" and "who is it acting
    # for". The identity layer must refuse a missing or repeated header.
    require("ApiError::MissingIdentity" in agent_auth, "the agent does not require an asserted identity")
    require("values.next().is_some()" in agent_auth, "a repeated identity header is not treated as ambiguous")

    agent_api = text("agent/src/api/mod.rs")
    require(
        "middleware::from_fn_with_state(key, auth::require_gateway)" in agent_api,
        "the agent's protected routes are not behind the gateway check",
    )
    require(
        '.route("/health", get(live))' in agent_api and ".fallback_service(protected)" in agent_api,
        "the agent's public and protected route split has changed",
    )

    # Identity enters in one place and the model cannot construct it.
    identity = text("agent/src/identity.rs")
    require(
        "pub(crate) fn from_verified_headers" in identity and "pub fn new" not in identity,
        "TrustedCaller gained a public constructor; identity must only come from verified headers",
    )

    # Every identity header the gateway mints needs an explicit telemetry
    # decision: never exported (IDENTITY_HEADERS) or deliberately kept
    # (RETAINED_GATEWAY_HEADERS). A new x-authenticated-* header in the gateway
    # fails here until it gets one.
    minted = set(re.findall(r'"(x-authenticated-[a-z-]+)"', proxy))
    require(bool(minted), "no x-authenticated-* headers found in gateway/src/proxy.rs")
    constants = dict(re.findall(r'pub const ([A-Z_]+): &str = "([a-z0-9-]+)";', identity))

    def header_list(name: str) -> set[str]:
        match = re.search(rf"pub const {name}: &\[&str\] = &\[(.*?)\];", identity, re.DOTALL)
        require(match is not None, f"identity.{name} is missing")
        return {constants[item.strip()] for item in match.group(1).split(",") if item.strip()}

    redacted = header_list("IDENTITY_HEADERS")
    retained = header_list("RETAINED_GATEWAY_HEADERS")
    require(not redacted & retained, f"headers both redacted and retained: {sorted(redacted & retained)}")
    undecided = minted - redacted - retained
    require(not undecided, f"gateway identity header(s) with no telemetry decision: {sorted(undecided)}")
    require(
        retained <= minted,
        f"RETAINED_GATEWAY_HEADERS names headers the gateway does not send: {sorted(retained - minted)}",
    )

    # Rig is pinned exactly: its API moves between minor releases.
    cargo = text("agent/Cargo.toml")
    for crate in ("rig-core", "rig-agent", "rig-rmcp", "rmcp"):
        require(
            re.search(rf'^{crate} = \{{ version = "=\d+\.\d+\.\d+"', cargo, re.MULTILINE) is not None,
            f"{crate} is not pinned to an exact version in agent/Cargo.toml",
        )

    # The MCP server is unchanged on this branch and stays the capability
    # boundary: same checks as on `main`.
    mcp = text("mcp-server/src/main.rs")
    require("MCP_API_KEY" in mcp, "MCP API-key environment variable missing")
    require("constant_time_eq" in mcp, "MCP key comparison is not constant time")
    require("request.headers_mut().remove(header::AUTHORIZATION)" in mcp, "MCP does not strip the service key")

    # The agent reaches MCP with the service credential, over MCP.
    mcp_client = text("agent/src/mcp/client.rs")
    require(".auth_header(" in mcp_client, "the agent does not send the MCP key")
    require("McpTool::from_mcp_server" in mcp_client, "the agent no longer invokes tools through rig-rmcp")

    # Tool policy is enforced at Rig's dispatch hook and re-checked by executors.
    hooks = text("agent/src/agent/hooks.rs")
    require("fn on_dispatch" in hooks and "tool_policy.decide" in hooks, "the tool-policy dispatch hook is missing")
    require("fn on_invalid_tool_call" in hooks, "unknown tool calls are not resolved deterministically")
    for executor in ("agent/src/mcp/tools.rs", "agent/src/agent/builder.rs"):
        require("tool_policy.decide" in text(executor), f"{executor} executes without re-checking the policy")

    # Only the authenticated route can build a human decision.
    pending = text("agent/src/approval/pending.rs")
    require(
        "pub struct VerifiedDecision(Decision);" in pending,
        "VerifiedDecision must keep a private field: only the interaction route may construct one",
    )
    require("RespondError::NotOwner" in pending, "interaction ownership is not checked")

    # The shipped agent configuration exposes no mutation tool.
    agent_config = text("agent/config.yml")
    require(
        re.search(r"^  approval:", agent_config, re.MULTILINE) is None,
        "agent/config.yml enables the approval tool by default",
    )

    evaluator = text("evaluation/client.py")
    require("AGENT_API_KEY" in evaluator, "evaluator does not require the agent key")

    # Every guardrail decision event the agent emits must be one the evaluator
    # recognises. These are magic strings shared across two deployed codebases,
    # and a mismatch is silent: the evaluator simply records nothing.
    guardrails_source = text("agent/src/agent/input_rail.rs") + text("agent/src/agent/execution.rs")
    emitted = set(re.findall(r'"(guardrail_[a-z0-9_]+_decision)"', guardrails_source))
    require(len(emitted) == 2, f"expected an input and an output decision event, found {sorted(emitted)}")
    # Read the evaluator's prefixes from source rather than importing it: this
    # check has to run with nothing but python3 installed, and evaluation.client
    # imports mlflow at module scope.
    prefixes = set(
        re.findall(
            r'^(?:INPUT|OUTPUT)_GUARDRAIL_EVENT_PREFIX = "([a-z0-9_]+)"',
            evaluator,
            re.MULTILINE,
        )
    )
    exact = set(
        re.findall(
            r'^(?:INPUT|OUTPUT)_GUARDRAIL_EVENT = "([a-z0-9_]+)"', evaluator, re.MULTILINE
        )
    )
    require(len(prefixes) == 2, "the evaluator no longer declares both event prefixes")
    for event in sorted(emitted):
        require(
            event in exact or any(event.startswith(prefix) for prefix in prefixes),
            f"the agent emits {event!r} but the evaluator would not capture it",
        )

    # /version must never be able to serve a credential or prompt text.
    provenance = text("agent/src/telemetry/provenance.rs")
    credential_reads = re.findall(
        r'(?:env::var|env!)\(\s*"([A-Z0-9_]*(?:API_KEY|SECRET|PASSWORD|TOKEN)[A-Z0-9_]*)"',
        provenance,
    )
    require(
        not credential_reads,
        f"provenance reads credential environment variable(s): {sorted(set(credential_reads))}",
    )
    require('"prompt_sha256"' in provenance, "provenance reports no prompt digest")
    require('"agent_runtime": AGENT_RUNTIME' in provenance, "provenance does not name the agent runtime")
    require(
        '"system_prompt":' not in provenance and '"prompt":' not in provenance,
        "provenance may expose prompt text rather than only its digest",
    )

    # What is exported to OTLP is fixed in code, not governed by RUST_LOG.
    telemetry = text("agent/src/telemetry/mod.rs")
    require(
        "with_filter(export_filter())" in telemetry and "rig=info" in telemetry,
        "the OTLP export filter no longer pins Rig to info (Rig logs full provider requests at TRACE)",
    )

    check_workflows()

    print("Security-critical source wiring passed.")


def check_workflows() -> None:
    """No free-text workflow input may reach a shell command.

    `${{ inputs.x }}` inside a `run:` block is textual substitution *before* the
    shell parses the script, so an input containing shell metacharacters executes
    on the runner with the job's token. `type: string` constrains nothing, and
    the only safe form is to pass the value through `env:` and reference it as a
    quoted shell variable.

    Enforced here rather than reviewed once, because the unsafe form is the
    obvious one to write and looks identical to the safe one at a glance.
    """

    workflows = ROOT / ".github" / "workflows"
    if not workflows.is_dir():
        return

    run_block = re.compile(r"run:\s*(\|[^\n]*\n(?:[ \t]+.*\n)*|.*\n)")
    interpolation = re.compile(r"\$\{\{\s*(?:inputs|github\.event)[^}]*\}\}")
    for workflow in sorted(workflows.glob("*.yml")):
        source = workflow.read_text(encoding="utf-8")
        for block in run_block.finditer(source):
            found = interpolation.findall(block.group(1))
            require(
                not found,
                f"{workflow.name} interpolates a workflow input into a shell "
                f"command: {found}. Pass it through `env:` and quote it instead.",
            )

    # Pull-request checks must stay runnable on a fork, which has no secrets.
    ci = workflows / "ci.yml"
    if ci.is_file():
        require(
            "secrets." not in ci.read_text(encoding="utf-8"),
            "ci.yml reads a secret; the pull-request gate must run without credentials",
        )

    # Artifact publication must be authorized by a validation outcome, not by
    # `if: always()`. A step whose `if:` is exactly `always()` runs whether or
    # not validation ran or passed, which is the bug this asserts is not back:
    # a failed or skipped validator must not be able to authorize an upload.
    live_eval = workflows / "live-evaluation.yml"
    if live_eval.is_file():
        source = live_eval.read_text(encoding="utf-8")
        require(
            "id: validate" in source,
            "live-evaluation.yml has no identifiable validation step to gate on",
        )
        upload_if = re.search(
            r"name:\s*Upload the results\s*\n\s*if:\s*(.+)", source
        )
        require(upload_if is not None, "could not find the upload step's `if:` condition")
        condition = upload_if.group(1).strip()
        require(
            condition != "always()",
            "the upload step is gated on always(), not on a validation outcome",
        )
        require(
            "steps.validate.outputs.eligible" in condition,
            f"the upload step's condition does not reference the validator's own "
            f"outcome: {condition!r}",
        )
        # A bare custom expression is not the whole condition GitHub Actions
        # evaluates: unless it contains always()/cancelled()/failure()/
        # success(), GitHub silently ANDs it with success() over the job so
        # far. That would make an earlier failed evaluation step block this
        # upload even when the validator legitimately approved a partial
        # result — reintroducing the bug this gate exists to close, just one
        # layer down. Also require the validator's own step *outcome*, not
        # only its output value, so a step that produced a stray "eligible"
        # output but otherwise failed still cannot authorize an upload.
        require(
            any(fn in condition for fn in ("always()", "cancelled()", "failure()", "success()")),
            f"the upload step's condition has no explicit status-check function, so GitHub "
            f"Actions silently ANDs it with success() over the whole job — an earlier failed "
            f"step would then block upload even when validation approved a partial result: "
            f"{condition!r}",
        )
        require(
            "steps.validate.outcome" in condition,
            f"the upload step's condition does not check the validator step's own outcome, "
            f"only its output value: {condition!r}",
        )


if __name__ == "__main__":
    main()
