#!/usr/bin/env python3
"""The shipped Compose configuration must ship no mutation surface.

Reads the resolved Compose configuration as JSON on stdin — e.g.::

    docker compose config --format json | python3 scripts/verify_read_only_default.py

Deliberately a file, not a ``python3 - <<'PY'`` heredoc: a heredoc redirects
stdin to the script text itself, which starves ``json.load(sys.stdin)`` of
the piped configuration and fails with ``JSONDecodeError`` before this check
ever runs. Passing the script as a file leaves stdin free for the pipe.
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    config = json.load(sys.stdin)
    services = config["services"]

    for name in ("agent", "mcp-server"):
        secret = services[name]["environment"].get("HITL_APPROVAL_SECRET", "")
        if secret:
            sys.exit(f"::error::{name} ships with HITL_APPROVAL_SECRET set")

    interactive = services["agent"]["environment"].get("HITL_ENABLE_INTERACTIVE", "false")
    if interactive.strip().lower() not in {"", "false", "0", "no", "off"}:
        sys.exit("::error::NAT interaction endpoints are enabled by default")

    print("The shipped configuration exposes no mutation surface.")


if __name__ == "__main__":
    main()
