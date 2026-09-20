#!/usr/bin/env python3
"""Validate the resolved Compose security topology from JSON on stdin."""

from __future__ import annotations

import json
import sys
from urllib.parse import urlparse


def fail(message: str) -> None:
    raise SystemExit(f"security configuration check failed: {message}")


def environment(service: dict) -> dict[str, str]:
    raw = service.get("environment", {})
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items()}
    result: dict[str, str] = {}
    if isinstance(raw, list):
        for item in raw:
            key, _, value = str(item).partition("=")
            result[key] = value
    return result


def main() -> None:
    config = json.load(sys.stdin)
    services = config.get("services") or {}
    for name in (
        "ui",
        "gateway",
        "keycloak",
        "keycloak-realm-init",
        "agent",
        "mcp-server",
        "evaluator",
    ):
        if name not in services:
            fail(f"missing service {name!r}")

    # Reachability from the host. These four services carry the credentials, the
    # model access and the data, and none of them may be addressable from outside
    # the cluster. `make network-test` proves the same property at runtime.
    for name in ("agent", "mcp-server", "gateway", "postgres"):
        if services.get(name, {}).get("ports"):
            fail(f"{name} publishes host ports in normal Compose")

    # East-west reachability. Each network is one trust relationship, so the
    # membership matrix is asserted directly: a service silently gaining a
    # network is how a boundary disappears without anything looking broken.
    def networks(name: str) -> set[str]:
        raw = services.get(name, {}).get("networks") or {}
        return set(raw) if isinstance(raw, dict) else set(raw)

    expected_networks = {
        "postgres": {"data_net"},
        "mcp-server": {"mcp_net", "data_net"},
        "agent": {"agent_net", "mcp_net", "telemetry_net"},
        "gateway": {"gateway_net", "auth_net", "agent_net"},
        "ui": {"edge", "gateway_net"},
        "keycloak": {"edge", "auth_net"},
        "keycloak-realm-init": {"auth_net"},
        "mlflow": {"edge", "telemetry_net"},
        "otel-collector": {"edge", "telemetry_net"},
        "evaluator": {"agent_net", "telemetry_net"},
    }
    for name, expected in expected_networks.items():
        if name not in services:
            continue
        actual = networks(name)
        if actual != expected:
            fail(
                f"{name} network membership changed: expected {sorted(expected)}, "
                f"found {sorted(actual)}"
            )

    # The consequences of that matrix, stated as the properties they protect.
    for consumer, target, shared in (
        ("ui", "the agent", "agent_net"),
        ("ui", "the MCP server", "mcp_net"),
        ("gateway", "the MCP server", "mcp_net"),
        ("gateway", "the database", "data_net"),
        ("agent", "the database", "data_net"),
    ):
        if shared in networks(consumer):
            fail(f"{consumer} can reach {target}; it must not share {shared}")

    # Only edge members may publish host ports.
    for name, service in services.items():
        if service.get("ports") and "edge" not in networks(name):
            fail(f"{name} publishes a host port without being on the edge network")

    gateway_env = environment(services["gateway"])
    realm_init_env = environment(services["keycloak-realm-init"])
    agent_env = environment(services["agent"])
    mcp_env = environment(services["mcp-server"])
    evaluator_env = environment(services["evaluator"])
    ui_env = environment(services["ui"])

    required = {
        "gateway AGENT_API_KEY": gateway_env.get("AGENT_API_KEY"),
        "agent NAT_GATEWAY_API_KEY": agent_env.get("NAT_GATEWAY_API_KEY"),
        "agent MCP_API_KEY": agent_env.get("MCP_API_KEY"),
        "MCP MCP_API_KEY": mcp_env.get("MCP_API_KEY"),
        "evaluator AGENT_API_KEY": evaluator_env.get("AGENT_API_KEY"),
        "gateway OIDC_CALLBACK_URL": gateway_env.get("OIDC_CALLBACK_URL"),
        "UI_PUBLIC_URL": gateway_env.get("UI_PUBLIC_URL"),
    }
    for label, value in required.items():
        if not value:
            fail(f"{label} is missing")

    if gateway_env["AGENT_API_KEY"] != agent_env["NAT_GATEWAY_API_KEY"]:
        fail("gateway and NAT use different AGENT_API_KEY values")
    if evaluator_env["AGENT_API_KEY"] != agent_env["NAT_GATEWAY_API_KEY"]:
        fail("evaluator and NAT use different AGENT_API_KEY values")
    if agent_env["MCP_API_KEY"] != mcp_env["MCP_API_KEY"]:
        fail("NAT and MCP use different MCP_API_KEY values")
    if ui_env.get("GATEWAY_INTERNAL_URL") != "http://gateway:8081":
        fail("assistant-ui does not target the internal Rust gateway")
    if realm_init_env.get("OIDC_CALLBACK_URL") != gateway_env["OIDC_CALLBACK_URL"]:
        fail("Keycloak and gateway use different OIDC callback URLs")

    ui_url = urlparse(gateway_env["UI_PUBLIC_URL"])
    callback_url = urlparse(gateway_env["OIDC_CALLBACK_URL"])
    if (ui_url.scheme, ui_url.netloc) != (callback_url.scheme, callback_url.netloc):
        fail("OIDC callback must use the assistant-ui origin")

    if callback_url.path != "/api/gateway/auth/callback":
        fail("OIDC callback path must be exactly /api/gateway/auth/callback")

    print("Resolved Compose security topology passed.")


if __name__ == "__main__":
    main()
