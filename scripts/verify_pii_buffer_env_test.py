#!/usr/bin/env python3
"""Verify GUARDRAILS_PII_MAX_BUFFER_CHARS reaches the agent container.

The Python code reads this variable and docs/GUARDRAILS.md documents its
default (200000), but until now docker-compose.yml never forwarded it into
the agent service's environment, so a deployment operator could set it in
their shell or .env and see it silently ignored. Render-only: no container
starts.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = "200000"


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _agent_env(env_overrides: dict | None = None) -> dict:
    env = dict(os.environ)
    env.update(env_overrides or {})
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker compose config failed: {result.stderr}")
    config = json.loads(result.stdout)
    return config["services"]["agent"]["environment"]


def _ensure_env_file() -> bool:
    env_path = ROOT / ".env"
    if env_path.exists():
        return False
    shutil.copyfile(ROOT / ".env.example", env_path)
    return True


def test_default_matches_documentation() -> None:
    env = _agent_env()
    assert env.get("GUARDRAILS_PII_MAX_BUFFER_CHARS") == DEFAULT, (
        f"expected the documented default {DEFAULT!r}, got "
        f"{env.get('GUARDRAILS_PII_MAX_BUFFER_CHARS')!r}"
    )
    print(f"PASS: the agent service's default GUARDRAILS_PII_MAX_BUFFER_CHARS is {DEFAULT}")


def test_explicit_override_reaches_the_agent_service() -> None:
    env = _agent_env({"GUARDRAILS_PII_MAX_BUFFER_CHARS": "4096"})
    assert env.get("GUARDRAILS_PII_MAX_BUFFER_CHARS") == "4096", (
        f"an explicit override did not reach the agent service's environment: {env.get('GUARDRAILS_PII_MAX_BUFFER_CHARS')!r}"
    )
    print("PASS: an explicit GUARDRAILS_PII_MAX_BUFFER_CHARS override reaches the agent service")


def main() -> None:
    if not _docker_available():
        print("SKIP: docker CLI not available; PII buffer env forwarding not exercised")
        return

    created = _ensure_env_file()
    try:
        test_default_matches_documentation()
        test_explicit_override_reaches_the_agent_service()
    finally:
        if created:
            (ROOT / ".env").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
