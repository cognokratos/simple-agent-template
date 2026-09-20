#!/usr/bin/env python3
"""Behavioral tests for the shipped-configuration read-only check.

These run the actual shell invocation CI uses — a subprocess piping JSON into
``python3 scripts/verify_read_only_default.py`` — rather than calling a Python
helper in isolation, so a regression to the ``python3 - <<'PY'`` stdin
conflict this script replaces would be caught here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_read_only_default.py"


def _services(**overrides) -> dict:
    agent_env = {"HITL_APPROVAL_SECRET": "", "HITL_ENABLE_INTERACTIVE": "false"}
    mcp_env = {"HITL_APPROVAL_SECRET": ""}
    agent_env.update(overrides.get("agent", {}))
    mcp_env.update(overrides.get("mcp-server", {}))
    return {"services": {"agent": {"environment": agent_env}, "mcp-server": {"environment": mcp_env}}}


def _run(payload: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )


def test_default_config_passes() -> None:
    result = _run(json.dumps(_services()))
    assert result.returncode == 0, result.stderr
    assert "exposes no mutation surface" in result.stdout
    print("PASS: the default (approvals off, interactive endpoints off) configuration passes")


def test_enabled_approval_secret_fails() -> None:
    result = _run(json.dumps(_services(agent={"HITL_APPROVAL_SECRET": "s3cr3t"})))
    assert result.returncode != 0
    assert "HITL_APPROVAL_SECRET" in (result.stdout + result.stderr)
    print("PASS: a configured HITL_APPROVAL_SECRET on the agent fails the check")


def test_enabled_approval_secret_on_mcp_fails() -> None:
    result = _run(json.dumps(_services(**{"mcp-server": {"HITL_APPROVAL_SECRET": "s3cr3t"}})))
    assert result.returncode != 0
    print("PASS: a configured HITL_APPROVAL_SECRET on the MCP server fails the check")


def test_enabled_interactive_endpoints_fail() -> None:
    result = _run(json.dumps(_services(agent={"HITL_ENABLE_INTERACTIVE": "true"})))
    assert result.returncode != 0
    assert "interaction endpoints" in (result.stdout + result.stderr)
    print("PASS: enabled interactive endpoints fail the check")


def test_empty_input_fails_clearly() -> None:
    result = _run("")
    assert result.returncode != 0
    print("PASS: empty input fails rather than silently passing")


def test_malformed_input_fails_clearly() -> None:
    result = _run("{not json")
    assert result.returncode != 0
    print("PASS: malformed input fails rather than silently passing")


def test_full_compose_pipeline_passes_on_the_shipped_default() -> None:
    """The exact command CI runs, against this repository's real compose files."""

    if shutil.which("docker") is None:
        print("SKIP: docker CLI not available; full Compose pipeline not exercised")
        return

    env_path = ROOT / ".env"
    created = not env_path.exists()
    if created:
        shutil.copyfile(ROOT / ".env.example", env_path)

    try:
        render = subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if render.returncode != 0:
            print(f"SKIP: `docker compose config` could not run here: {render.stderr.strip()[:200]}")
            return

        result = _run(render.stdout)
        assert result.returncode == 0, result.stderr
        assert "exposes no mutation surface" in result.stdout
        print("PASS: `docker compose config --format json | python3 scripts/verify_read_only_default.py` "
              "passes on the shipped default")
    finally:
        if created:
            env_path.unlink(missing_ok=True)


def main() -> None:
    test_default_config_passes()
    test_enabled_approval_secret_fails()
    test_enabled_approval_secret_on_mcp_fails()
    test_enabled_interactive_endpoints_fail()
    test_empty_input_fails_clearly()
    test_malformed_input_fails_clearly()
    test_full_compose_pipeline_passes_on_the_shipped_default()


if __name__ == "__main__":
    main()
