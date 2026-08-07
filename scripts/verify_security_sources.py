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

    gateway = text("gateway/src/main.rs")
    for route in (
        '.route("/health", get(health))',
        '.route("/ready", get(ready))',
        '.route("/auth/login", get(login))',
        '.route("/auth/callback", get(callback))',
        '.route("/auth/session", get(auth_session))',
        '.route("/auth/logout", post(logout))',
        '.route("/api/chat", post(chat))',
    ):
        require(route in gateway, f"missing fixed gateway route {route}")
    require('const BROWSER_COOKIE_PATH: &str = "/api/gateway"' in gateway, "gateway cookie path is not fixed to /api/gateway")
    require('const OIDC_CALLBACK_PATH: &str = "/api/gateway/auth/callback"' in gateway, "gateway callback path is not fixed to the UI callback")
    require("OIDC_CALLBACK_URL" in gateway, "gateway callback URL is not explicit")
    require("SameSite=Lax" in gateway and "SameSite=Strict" in gateway, "cookie SameSite flags missing")
    require("HttpOnly" in gateway, "session/login cookies are not HttpOnly")
    require("verify_csrf" in gateway, "CSRF enforcement is missing")
    require("Policy::none()" in gateway, "gateway HTTP redirects are not disabled")

    nat_patch = text("agent/patch_nat_api_key.py")
    require("NAT_GATEWAY_API_KEY" in nat_patch, "NAT API-key environment variable missing")
    require('request.scope["headers"] = sanitized_headers' in nat_patch, "NAT does not strip the service key")

    mcp = text("mcp-server/src/main.rs")
    require("MCP_API_KEY" in mcp, "MCP API-key environment variable missing")
    require("constant_time_eq" in mcp, "MCP key comparison is not constant time")
    require("request.headers_mut().remove(header::AUTHORIZATION)" in mcp, "MCP does not strip the service key")

    agent_config = text("agent/config.yml")
    require("custom_headers:" in agent_config, "NAT MCP custom headers are missing")
    require("Authorization: Bearer ${MCP_API_KEY}" in agent_config, "NAT does not send the MCP key")

    evaluator = text("evaluation/client.py")
    require("AGENT_API_KEY" in evaluator, "evaluator does not require the NAT key")

    print("Security-critical source wiring passed.")


if __name__ == "__main__":
    main()
