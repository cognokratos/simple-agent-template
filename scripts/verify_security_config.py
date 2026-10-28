#!/usr/bin/env python3
"""Validate the resolved Compose security topology from JSON on stdin.

Run against every render that matters (`make security-config-test`): the
default stack with the evaluation and dev profiles, and the same with the
optional `mlflow` profile. A service that only exists under a profile is
checked whenever that profile is rendered.
"""

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
        "otel-collector",
        "phoenix",
        "evaluator",
    ):
        if name not in services:
            fail(f"missing service {name!r}")

    # The observability default is Phoenix; MLflow is opt-in. A render without
    # the mlflow profile must not contain it, or "optional" means nothing.
    profiles_of = {name: set(service.get("profiles") or []) for name, service in services.items()}
    # The evaluators run on demand (`docker compose run`), never on `up`.
    for name, profile in (("mlflow", "mlflow"), ("evaluator", "evaluation"), ("evaluator-mlflow", "evaluation-mlflow")):
        if name in services and profiles_of[name] != {profile}:
            fail(f"{name} must be gated by exactly the {profile!r} profile, found {sorted(profiles_of[name])}")
    if profiles_of.get("phoenix"):
        fail("phoenix must be part of the default stack, not behind a profile")
    phoenix_image = str(services["phoenix"].get("image", ""))
    if "@sha256:" not in phoenix_image or ":latest" in phoenix_image or ":version-" not in phoenix_image:
        fail(f"phoenix image must be pinned by version and digest, found {phoenix_image!r}")

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
        "phoenix": {"edge", "observability_net"},
        "mlflow": {"edge", "observability_net"},
        "otel-collector": {"edge", "telemetry_net", "observability_net"},
        "evaluator": {"agent_net", "observability_net"},
        "evaluator-mlflow": {"agent_net", "observability_net"},
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

    # Observability backends render UIs to the browser and, in both cases, can
    # make outbound requests on a user's behalf (LLM playgrounds). They must
    # share no network with a protected service, so neither can become a
    # browser-driven path to the agent, MCP, the gateway or the database.
    protected = ("agent", "mcp-server", "gateway", "postgres")
    for backend in ("phoenix", "mlflow"):
        if backend not in services:
            continue
        for target in protected:
            shared_networks = networks(backend) & networks(target)
            if shared_networks:
                fail(f"{backend} shares {sorted(shared_networks)} with {target}; it must not reach it")

    # The agent's telemetry network carries trace export to the collector and
    # nothing else.
    telemetry_members = {name for name in services if "telemetry_net" in networks(name)}
    if telemetry_members != {"agent", "otel-collector"}:
        fail(f"telemetry_net must hold only the agent and the collector, found {sorted(telemetry_members)}")

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
        "agent AGENT_API_KEY": agent_env.get("AGENT_API_KEY"),
        "agent MCP_API_KEY": agent_env.get("MCP_API_KEY"),
        "MCP MCP_API_KEY": mcp_env.get("MCP_API_KEY"),
        "evaluator AGENT_API_KEY": evaluator_env.get("AGENT_API_KEY"),
        "gateway OIDC_CALLBACK_URL": gateway_env.get("OIDC_CALLBACK_URL"),
        "UI_PUBLIC_URL": gateway_env.get("UI_PUBLIC_URL"),
    }
    for label, value in required.items():
        if not value:
            fail(f"{label} is missing")

    if gateway_env["AGENT_API_KEY"] != agent_env["AGENT_API_KEY"]:
        fail("gateway and agent use different AGENT_API_KEY values")
    if evaluator_env["AGENT_API_KEY"] != agent_env["AGENT_API_KEY"]:
        fail("evaluator and agent use different AGENT_API_KEY values")
    if "evaluator-mlflow" in services:
        if environment(services["evaluator-mlflow"]).get("AGENT_API_KEY") != agent_env["AGENT_API_KEY"]:
            fail("evaluator-mlflow and agent use different AGENT_API_KEY values")

    # Credentials never travel to an observability backend's configuration:
    # Phoenix and MLflow are display surfaces and receive no service key.
    secret_values = {
        value
        for value in (agent_env.get("AGENT_API_KEY"), agent_env.get("MCP_API_KEY"), agent_env.get("HITL_APPROVAL_SECRET"))
        if value
    }
    for backend in ("phoenix", "mlflow", "otel-collector"):
        if backend in services:
            leaked = [key for key, value in environment(services[backend]).items() if value in secret_values]
            if leaked:
                fail(f"{backend} receives a service credential in {leaked}")
    # The container receives only the current names; Compose resolves the
    # deprecated host-side aliases (docs/CONFIGURATION.md#renamed-variables).
    for deprecated in ("NAT_GATEWAY_API_KEY", "NAT_TRACE_CAPTURE_CONTENT", "NAT_TRACE_CONTENT_MAX_CHARS"):
        if deprecated in agent_env:
            fail(f"the agent container still receives the deprecated {deprecated}")
    if agent_env["MCP_API_KEY"] != mcp_env["MCP_API_KEY"]:
        fail("agent and MCP use different MCP_API_KEY values")
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
