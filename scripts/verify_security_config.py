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


def networks_of(service: dict) -> set[str]:
    raw = service.get("networks")
    if isinstance(raw, dict):
        return set(raw.keys())
    if isinstance(raw, list):
        return {str(item) for item in raw}
    return set()


#: Exact expected network membership. Adding a service to a network is how a new
#: east-west path is opened, so it must be a deliberate, reviewed change.
EXPECTED_NETWORKS: dict[str, set[str]] = {
    "ui": {"edge", "gateway_net"},
    "gateway": {"gateway_net", "auth_net", "agent_net"},
    "agent": {"agent_net", "mcp_net", "telemetry_net"},
    "mcp-server": {"mcp_net", "data_net"},
    "postgres": {"data_net"},
    "keycloak": {"edge", "auth_net"},
    "keycloak-realm-init": {"auth_net"},
    "mcp-inspector": {"edge", "mcp_net"},
    "mlflow": {"edge", "telemetry_net"},
    "otel-collector": {"edge", "telemetry_net"},
    "evaluator": {"agent_net", "telemetry_net"},
}


def check_network_topology(config: dict, services: dict) -> None:
    """Assert the east-west topology, not just the absence of host ports."""

    if "default" in (config.get("networks") or {}):
        fail("a flat default network is back; every service must declare its networks")

    for name, expected in EXPECTED_NETWORKS.items():
        actual = networks_of(services[name])
        if actual != expected:
            fail(f"{name} joins {sorted(actual)}; expected {sorted(expected)}")

    # The properties the topology exists to guarantee.
    agent_reachable_from = {
        name for name, service in services.items() if "agent_net" in networks_of(service)
    }
    if agent_reachable_from != {"agent", "gateway", "evaluator"}:
        fail(f"NAT is reachable from unexpected services: {sorted(agent_reachable_from - {'agent'})}")

    mcp_reachable_from = {
        name for name, service in services.items() if "mcp_net" in networks_of(service)
    }
    if mcp_reachable_from != {"mcp-server", "agent", "mcp-inspector"}:
        fail(f"MCP is reachable from unexpected services: {sorted(mcp_reachable_from - {'mcp-server'})}")

    db_reachable_from = {
        name for name, service in services.items() if "data_net" in networks_of(service)
    }
    if db_reachable_from != {"postgres", "mcp-server"}:
        fail(f"PostgreSQL is reachable from unexpected services: {sorted(db_reachable_from - {'postgres'})}")

    # assistant-ui is the most exposed container: it is browser-facing, so an SSRF
    # there must not reach the agent runtime, the tools, or the database.
    ui_networks = networks_of(services["ui"])
    for forbidden in ("agent_net", "mcp_net", "data_net"):
        if forbidden in ui_networks:
            fail(f"assistant-ui must not join {forbidden}")

    published = {
        name for name, service in services.items() if service.get("ports")
    }
    for name in published:
        if "edge" not in networks_of(services[name]):
            fail(f"{name} publishes host ports but is not on the edge network")


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
        "mcp-inspector",
        "evaluator",
    ):
        if name not in services:
            fail(f"missing service {name!r}")

    for name in ("agent", "mcp-server", "gateway", "postgres"):
        if services[name].get("ports"):
            fail(f"{name} publishes host ports in normal Compose")

    check_network_topology(config, services)

    gateway_env = environment(services["gateway"])
    realm_init_env = environment(services["keycloak-realm-init"])
    agent_env = environment(services["agent"])
    mcp_env = environment(services["mcp-server"])
    evaluator_env = environment(services["evaluator"])
    inspector_env = environment(services["mcp-inspector"])
    ui_env = environment(services["ui"])

    required = {
        "gateway AGENT_API_KEY": gateway_env.get("AGENT_API_KEY"),
        "agent NAT_GATEWAY_API_KEY": agent_env.get("NAT_GATEWAY_API_KEY"),
        "agent MCP_API_KEY": agent_env.get("MCP_API_KEY"),
        "MCP MCP_API_KEY": mcp_env.get("MCP_API_KEY"),
        "Inspector MCP_API_KEY": inspector_env.get("MCP_API_KEY"),
        "Inspector API token": inspector_env.get("MCP_INSPECTOR_API_TOKEN"),
        "evaluator AGENT_API_KEY": evaluator_env.get("AGENT_API_KEY"),
        "gateway OIDC_CALLBACK_URL": gateway_env.get("OIDC_CALLBACK_URL"),
        "agent HITL_APPROVAL_SECRET": agent_env.get("HITL_APPROVAL_SECRET"),
        "MCP HITL_APPROVAL_SECRET": mcp_env.get("HITL_APPROVAL_SECRET"),
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
    if inspector_env["MCP_API_KEY"] != mcp_env["MCP_API_KEY"]:
        fail("MCP Inspector and MCP use different MCP_API_KEY values")
    if agent_env["HITL_APPROVAL_SECRET"] != mcp_env["HITL_APPROVAL_SECRET"]:
        fail("NAT and MCP use different HITL_APPROVAL_SECRET values")
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

    inspector_ports = services["mcp-inspector"].get("ports") or []
    if not inspector_ports:
        fail("MCP Inspector does not publish its local development UI")
    for port in inspector_ports:
        host_ip = str(port.get("host_ip") or "") if isinstance(port, dict) else str(port)
        if host_ip not in {"127.0.0.1", "::1"}:
            fail(f"MCP Inspector must be loopback-only, got host_ip={host_ip!r}")

    print("Resolved Compose security topology passed.")


if __name__ == "__main__":
    main()
