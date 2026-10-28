#!/usr/bin/env python3
"""Behavioral tests for the observability profiles in docker-compose.yml.

Only renders configuration; starts nothing. Renders from ``.env.example`` with
the relevant variables removed from the environment, so a developer's own
``.env`` cannot make the shipped defaults look different from what they are.
Skips cleanly when the ``docker`` CLI is unavailable.

Asserted:

* Phoenix is in the default stack; MLflow and its evaluator are not, and appear
  only with ``--profile mlflow``;
* enabling MLflow changes no protected service's reachability, and the
  topology validator passes for both renders;
* the validator rejects an observability backend that could reach the agent,
  MCP, the gateway or the database, or an MLflow that lost its profile;
* the collector's default overlay adds nothing, and the MLflow overlay is the
  only way MLflow receives traces;
* raw/pre-rail content capture and per-user attribution are off by default.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "verify_security_config.py"
PROTECTED = ("agent", "mcp-server", "gateway", "postgres")
SCRUBBED_ENV = (
    "COMPOSE_PROFILES",
    "OTEL_COLLECTOR_OVERLAY",
    "AGENT_TRACE_CAPTURE_MODEL_CONTENT",
    "GUARDRAILS_TRACE_CAPTURE_RAW_OUTPUT",
    "OTEL_TRACE_USER_ID",
    "AGENT_TRACE_CAPTURE_CONTENT",
    "NAT_TRACE_CAPTURE_CONTENT",
)


def _render(*profiles: str, env: dict | None = None) -> dict:
    environment = {key: value for key, value in os.environ.items() if key not in SCRUBBED_ENV}
    environment.update(env or {})
    args = [arg for profile in profiles for arg in ("--profile", profile)]
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(ROOT / ".env.example"), *args, "config", "--format", "json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker compose config failed: {result.stderr}")
    return json.loads(result.stdout)


def _validate(config: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(VALIDATOR)], input=json.dumps(config), capture_output=True, text=True, check=False
    )


def _networks(config: dict, name: str) -> set[str]:
    raw = config["services"].get(name, {}).get("networks") or {}
    return set(raw) if isinstance(raw, dict) else set(raw)


def _reachable(config: dict, name: str) -> set[str]:
    """Services sharing at least one network with ``name``."""
    mine = _networks(config, name)
    return {other for other in config["services"] if other != name and mine & _networks(config, other)}


def test_phoenix_is_default_and_mlflow_is_opt_in() -> None:
    default = _render()
    assert "phoenix" in default["services"]
    assert "mlflow" not in default["services"]
    assert "evaluator-mlflow" not in default["services"]
    assert "mlflow-data" not in (default.get("volumes") or {})
    with_mlflow = _render("mlflow")
    assert "mlflow" in with_mlflow["services"]
    # Bringing the MLflow stack up must not start an evaluation run.
    assert "evaluator-mlflow" not in with_mlflow["services"]
    assert "evaluator-mlflow" in _render("mlflow", "evaluation-mlflow")["services"]
    print("PASS: Phoenix is in the default stack; MLflow appears only with --profile mlflow; no evaluator starts on up")


def test_mlflow_changes_no_protected_reachability() -> None:
    base = _render("evaluation", "dev")
    with_mlflow = _render("evaluation", "dev", "mlflow", "evaluation-mlflow")
    for name in PROTECTED:
        before = _reachable(base, name)
        after = _reachable(with_mlflow, name) - {"evaluator-mlflow"}
        assert before == after, f"enabling mlflow changed who reaches {name}: {sorted(after ^ before)}"
    # The extra evaluator reaches the agent exactly as the default one does.
    assert _networks(with_mlflow, "evaluator-mlflow") == _networks(base, "evaluator")
    for backend in ("phoenix", "mlflow"):
        assert not (_reachable(with_mlflow, backend) & set(PROTECTED)), backend
    print("PASS: enabling MLflow changes no protected service's reachability")


def test_the_validator_passes_both_renders() -> None:
    for profiles in (("evaluation", "dev"), ("evaluation", "dev", "mlflow", "evaluation-mlflow")):
        result = _validate(_render(*profiles))
        assert result.returncode == 0, (profiles, result.stdout, result.stderr)
    print("PASS: the topology validator accepts the default and the MLflow renders")


def test_the_validator_rejects_a_backend_that_reaches_a_protected_service() -> None:
    config = _render("evaluation", "dev", "mlflow", "evaluation-mlflow")
    for backend, network in (("phoenix", "agent_net"), ("phoenix", "telemetry_net"), ("mlflow", "mcp_net")):
        mutated = copy.deepcopy(config)
        mutated["services"][backend]["networks"][network] = {}
        result = _validate(mutated)
        assert result.returncode != 0, f"{backend} joining {network} was accepted"
    unprofiled = copy.deepcopy(config)
    unprofiled["services"]["mlflow"]["profiles"] = []
    assert _validate(unprofiled).returncode != 0, "an always-on MLflow was accepted"
    auto_run = copy.deepcopy(config)
    auto_run["services"]["evaluator-mlflow"]["profiles"] = ["mlflow"]
    assert _validate(auto_run).returncode != 0, "an evaluator started by `up` was accepted"
    floating = copy.deepcopy(config)
    floating["services"]["phoenix"]["image"] = "arizephoenix/phoenix:latest"
    assert _validate(floating).returncode != 0, "an unpinned Phoenix image was accepted"
    print("PASS: the validator rejects a backend on a protected network, an unprofiled MLflow, a floating image")


def test_the_collector_exports_to_mlflow_only_with_its_overlay() -> None:
    default_command = _render("mlflow")["services"]["otel-collector"]["command"]
    assert default_command[-1].endswith("/otel-collector.default.yml"), default_command
    overlay = _render("mlflow", env={"OTEL_COLLECTOR_OVERLAY": "otel-collector.mlflow.yml"})
    assert overlay["services"]["otel-collector"]["command"][-1].endswith("/otel-collector.mlflow.yml")
    base = (ROOT / "observability" / "otel-collector.yml").read_text(encoding="utf-8")
    noop = (ROOT / "observability" / "otel-collector.default.yml").read_text(encoding="utf-8")
    assert "mlflow:5000" not in base, "the base collector config must not export to MLflow"
    assert all(not line.strip() or line.lstrip().startswith("#") for line in noop.splitlines()), (
        "the default overlay must add nothing"
    )
    print("PASS: the collector exports to MLflow only through its overlay")


def test_content_capture_and_attribution_defaults_are_private() -> None:
    agent = _render()["services"]["agent"]["environment"]
    # Pre-output-rail model text and raw guardrail output: never by default.
    assert agent["AGENT_TRACE_CAPTURE_MODEL_CONTENT"] == "false"
    assert agent["GUARDRAILS_TRACE_CAPTURE_RAW_OUTPUT"] == "false"
    # Per-user attribution (a pseudonym even when on): off by default.
    assert agent["OTEL_TRACE_USER_ID"] == "false"
    print("PASS: raw content capture and user attribution are off by default")


def main() -> None:
    if shutil.which("docker") is None:
        print("SKIP: docker CLI not available; Compose profile checks not exercised")
        return
    test_phoenix_is_default_and_mlflow_is_opt_in()
    test_mlflow_changes_no_protected_reachability()
    test_the_validator_passes_both_renders()
    test_the_validator_rejects_a_backend_that_reaches_a_protected_service()
    test_the_collector_exports_to_mlflow_only_with_its_overlay()
    test_content_capture_and_attribution_defaults_are_private()


if __name__ == "__main__":
    main()
