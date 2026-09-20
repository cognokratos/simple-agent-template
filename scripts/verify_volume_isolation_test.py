#!/usr/bin/env python3
"""Behavioral tests for Compose project/volume isolation.

Two projects on the same Docker host must not collide on persistent storage.
An unconditional literal default on `docker-compose.yml`'s four named volumes
would let changing `COMPOSE_PROJECT_NAME` or using `docker compose -p`
separate containers and networks while still leaving every project pointing
at the exact same volumes.

These tests only render configuration (`docker compose config`); nothing here
starts a container, touches a running database, or performs a destructive
operation. Skips cleanly if the `docker` CLI is unavailable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VOLUME_KEYS = ("postgres-data", "mlflow-data", "keycloak-data", "keycloak-import")


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _render(*extra_args: str, env_overrides: dict | None = None) -> dict:
    import os

    env = dict(os.environ)
    env.update(env_overrides or {})
    result = subprocess.run(
        ["docker", "compose", *extra_args, "config", "--format", "json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker compose config failed: {result.stderr}")
    return json.loads(result.stdout)


def _volume_names(config: dict) -> dict:
    return {key: config["volumes"][key].get("name") for key in VOLUME_KEYS}


def _ensure_env_file() -> bool:
    env_path = ROOT / ".env"
    if env_path.exists():
        return False
    shutil.copyfile(ROOT / ".env.example", env_path)
    return True


def test_default_matches_the_documented_project_scoped_name() -> None:
    """An unconfigured deployment gets the documented `tickets-agent-*` volumes."""

    config = _render()
    names = _volume_names(config)
    for key in VOLUME_KEYS:
        assert names[key] == f"tickets-agent-{key}", names
    print("PASS: the unconfigured default matches the documented project-scoped name")


def test_compose_project_name_isolates_every_volume() -> None:
    a = _volume_names(_render(env_overrides={"COMPOSE_PROJECT_NAME": "proj-a"}))
    b = _volume_names(_render(env_overrides={"COMPOSE_PROJECT_NAME": "proj-b"}))
    for key in VOLUME_KEYS:
        assert a[key] != b[key], f"{key} did not change between projects: {a[key]!r}"
        assert a[key] == f"proj-a-{key}"
        assert b[key] == f"proj-b-{key}"
    print("PASS: COMPOSE_PROJECT_NAME isolates every persistent volume by default")


def test_dash_p_isolates_every_volume() -> None:
    """`-p` alone (no COMPOSE_PROJECT_NAME) must isolate storage too.

    This is the exact case the finding describes as broken: `-p` separates
    containers and networks via Compose's own project scoping, but a literal
    `name:` on a volume bypasses that scoping entirely.
    """

    a = _volume_names(_render("-p", "proj-x"))
    b = _volume_names(_render("-p", "proj-y"))
    for key in VOLUME_KEYS:
        assert a[key] != b[key], f"-p did not isolate {key}: {a[key]!r}"
        assert a[key] == f"proj-x-{key}"
        assert b[key] == f"proj-y-{key}"
    print("PASS: `docker compose -p` isolates every persistent volume by default")


def test_explicit_override_wins_and_is_not_project_scoped() -> None:
    config = _render(env_overrides={"COMPOSE_PROJECT_NAME": "proj-a", "POSTGRES_DATA_VOLUME": "pinned-pg-data"})
    names = _volume_names(config)
    assert names["postgres-data"] == "pinned-pg-data"
    # The other three volumes are untouched by the override and still isolate.
    assert names["mlflow-data"] == "proj-a-mlflow-data"
    print("PASS: an explicit volume-name override wins over the computed project-scoped default")


def test_profiles_still_render() -> None:
    for args in ([], ["--profile", "evaluation"], ["--profile", "dev"], ["--profile", "evaluation", "--profile", "dev"]):
        subprocess.run(
            ["docker", "compose", *args, "config", "-q"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    print("PASS: the default, evaluation and dev profiles all still render")


def main() -> None:
    if not _docker_available():
        print("SKIP: docker CLI not available; Compose volume-isolation checks not exercised")
        return

    created = _ensure_env_file()
    try:
        test_default_matches_the_documented_project_scoped_name()
        test_compose_project_name_isolates_every_volume()
        test_dash_p_isolates_every_volume()
        test_explicit_override_wins_and_is_not_project_scoped()
        test_profiles_still_render()
    finally:
        if created:
            (ROOT / ".env").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
