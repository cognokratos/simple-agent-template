# SPDX-License-Identifier: Apache-2.0
"""What this agent actually is, so an evaluation result can name it.

An evaluation artifact that records its dataset, its metrics and its latency but
not the agent that produced them is not evidence of anything reproducible:
``evaluation_correct = 1.0`` certifies a system nobody can identify afterwards. This
module supplies the missing half.

Why the *agent* reports this rather than the harness reading the repository
-------------------------------------------------------------------------
The obvious implementation is to stamp ``git rev-parse HEAD`` from the host into
the result file. That records the working tree, and the working tree is not what
was measured: editing ``config.yml`` without ``make rebuild-agent`` leaves a
container running the previous prompt, and a host commit would then attest to
code that was never evaluated. It is the same failure the rules baseline avoids
by emitting its artifact from the production code path.

So the running process reports its own identity, the evaluator records what it
was told, and a disagreement between the two is published rather than hidden.

Why hashes and not the prompt text
----------------------------------
The system prompt instructs the model never to reveal hidden prompts. An endpoint
that served them would contradict that instruction from the other side, and would
put prompt text into every evaluation artifact and MLflow run. A digest is
sufficient for the only question being asked — *is this the same prompt?* — and
the evaluator holds the file itself, so it can register the text in MLflow's
prompt registry and compare its own digest against the one reported here.

The endpoint is authenticated. It is deliberately absent from
``fastapi_worker.PUBLIC_PATHS``: the evaluator already holds the service
credential, so there is no reason to widen the unauthenticated surface.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

#: The config NAT was started with. ``CMD`` passes ``--config_file /app/config.yml``.
CONFIG_PATH = Path(os.environ.get("NAT_CONFIG_PATH", "/app/config.yml"))

#: Set from a Docker build argument, so it describes the *image*, not the host.
BUILD_COMMIT_ENV = "AGENT_BUILD_COMMIT"

_cached: dict[str, Any] | None = None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_sha256(value: Any) -> str:
    """Digest a parsed YAML fragment, not its formatting.

    Dumped with sorted keys so that reordering or reindenting the source file
    does not read as a change to the prompt it contains.
    """
    return _sha256(yaml.safe_dump(value, sort_keys=True, default_flow_style=False))


def _dig(config: dict[str, Any], *path: str) -> Any:
    node: Any = config
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def describe(config_path: Path | None = None, *, refresh: bool = False) -> dict[str, Any]:
    """Identify this agent: image build, prompt digests, model binding, tools.

    Degrades rather than raises. Provenance failing must not be able to fail an
    evaluation run, so an unreadable config yields ``available: false`` and a
    reason, which the evaluator records as-is.
    """
    global _cached
    if _cached is not None and not refresh and config_path is None:
        return _cached

    path = config_path or CONFIG_PATH
    identity: dict[str, Any] = {
        "available": False,
        # Runtime binding, which the config file only templates. Reported
        # separately from the config digest for exactly that reason: the same
        # config can be pointed at a different model without changing its hash.
        "model": os.environ.get("LLM_MODEL", ""),
        "guard_model": os.environ.get("LLM_GUARD_MODEL", ""),
        "reasoning_effort": os.environ.get("LLM_REASONING_EFFORT", ""),
        "guard_reasoning_effort": os.environ.get("LLM_GUARD_REASONING_EFFORT", ""),
        "build_commit": os.environ.get(BUILD_COMMIT_ENV, "unknown"),
    }

    try:
        raw = path.read_text(encoding="utf-8")
        config = yaml.safe_load(raw)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        logger.warning("Agent provenance unavailable: %s", exc)
        identity["error"] = f"{type(exc).__name__}: {exc}"
        return identity

    system_prompt = _dig(config, "workflow", "system_prompt")
    rail_prompts = _dig(config, "middleware", "workflow_guardrails", "guardrails", "prompts")
    include = _dig(config, "function_groups", "etf_mcp", "include")

    identity.update({
        "available": True,
        "config_sha256": _sha256(raw),
        "prompt_sha256": _sha256(system_prompt) if isinstance(system_prompt, str) else None,
        "guardrails_prompts_sha256": canonical_sha256(rail_prompts) if rail_prompts else None,
        # The capability surface. A mutation tool appearing here would be a
        # finding on its own, so the evaluation records it per run.
        "tools_exposed": sorted(include) if isinstance(include, list) else [],
    })

    if config_path is None:
        _cached = identity
    return identity
