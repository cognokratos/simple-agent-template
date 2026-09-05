"""Link every evaluation run to the agent and prompt version that produced it.

Without this, a result file records how a number was produced and nothing about
what was measured. ``evaluation_correct = 1.0`` names its dataset but not the agent,
so the number cannot be attributed to a prompt, a model or a build afterwards.

Three identities are collected, from three different places, because no single
place knows all of them:

``agent``
    Reported by the running container over its authenticated ``GET /version``.
    This is the only source that describes what actually answered the questions.
``prompts``
    Registered in MLflow's prompt registry from ``agent/config.yml``, mounted
    read-only. Loading a registered prompt inside an active run makes MLflow
    record the exact version on the run, so the link is MLflow's own rather than
    a tag we maintain. Real versions beat a digest: a version can be diffed.
``harness``
    The source commit of this repository, passed in by the Makefile. The evaluator
    container has no ``.git`` and no working tree, so ``get_git_commit(".")``
    inside it would return ``None`` and MLflow's git versioning would report
    ``local-dev``. The host is the only place that can answer.

    ``source`` records *where* that answer came from:

    ``git``
        a real checkout; ``dirty`` was observed and is a true boolean.
    ``unknown``
        no git tree was available — an exported tarball, a container build
        context — so the commit is honestly ``unknown``.

    ``dirty`` is therefore ``bool | None``, and ``None`` means "not observable"
    rather than "clean". Equating those two would let a tree nobody inspected
    claim a verified clean checkout.

The interesting field is ``consistent``. The evaluator registers the prompt from
the *file* while the agent reports a digest of the prompt it *loaded*; if those
disagree, the container is running something other than the working tree — the
exact drift a host-side git stamp would have concealed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import mlflow
import yaml

logger = logging.getLogger(__name__)

#: Mounted read-only by the evaluator service.
AGENT_CONFIG_PATH = Path(os.environ.get("AGENT_CONFIG_PATH", "/app/agent-config.yml"))

PROMPT_NAME = os.getenv("EVALUATION_SYSTEM_PROMPT_NAME", "etf-research-agent-system-prompt")
RAIL_PROMPT_NAME = os.getenv("EVALUATION_RAIL_PROMPT_NAME", "etf-research-guardrail-input-rail")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_sha256(value: Any) -> str:
    """Mirror of ``nat_streaming_react.provenance.canonical_sha256``.

    Duplicated rather than imported: the evaluator image does not install the
    agent package, and mounting the agent source into the harness to share one
    helper would be a worse trade than two short identical functions.

    The duplication is not taken on trust. Both sides digest the same file, so
    every run compares the two results and reports ``prompt_matches_config`` /
    ``guardrail_prompts_match_config``; if either helper drifts, or the two
    containers resolve a different PyYAML, the next run says so instead of
    quietly agreeing. ``make eval-test`` additionally pins this helper's output
    for a fixed fragment so an accidental edit here fails offline.
    """
    return _sha256(yaml.safe_dump(value, sort_keys=True, default_flow_style=False))


def _version_url() -> str:
    explicit = os.getenv("AGENT_VERSION_URL")
    if explicit:
        return explicit
    workflow = os.getenv("AGENT_WORKFLOW_URL", "http://agent:8000/v1/workflow/full")
    return workflow.split("/v1/")[0] + "/version"


def agent_identity(timeout: float = 10.0) -> dict[str, Any]:
    """Ask the running agent what it is. Never raises."""
    url = _version_url()
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {os.getenv('AGENT_API_KEY', '')}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning("Agent provenance unavailable from %s: %s", url, exc)
        return {"available": False, "error": f"{type(exc).__name__}: {exc}", "url": url}


#: Values ``GIT_SOURCE`` may take, mirroring the Makefile's two branches.
SOURCE_GIT = "git"
SOURCE_UNKNOWN = "unknown"

_TRUE = {"1", "true", "yes"}
_FALSE = {"0", "false", "no"}


def _tri_state(raw: str) -> bool | None:
    """Parse a flag that is allowed to be unobserved.

    Empty means the Makefile had no git tree to examine. It must not collapse to
    ``False``, which would assert a clean tree nobody looked at.
    """
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return None


def harness_identity() -> dict[str, Any]:
    """The source identity the Makefile resolved on the host."""
    commit = os.getenv("GIT_COMMIT", "").strip() or "unknown"
    source = os.getenv("GIT_SOURCE", "").strip().lower()
    if source not in {SOURCE_GIT, SOURCE_UNKNOWN}:
        # Unset or unrecognised: infer conservatively rather than guess a source.
        # A commit with no stated origin predates this field, and only a git
        # checkout could have produced one.
        source = SOURCE_GIT if commit != "unknown" else SOURCE_UNKNOWN

    return {
        "git_commit": commit,
        "source": source,
        # A dirty tree is published rather than smoothed over: it means the commit
        # alone does not fully describe what was evaluated. None means no git tree
        # was available to inspect — see the module docstring.
        "dirty": _tri_state(os.getenv("GIT_DIRTY", "")),
    }


def _config_prompts(path: Path | None = None) -> dict[str, Any]:
    """Read the prompt texts the agent is configured with."""
    target = path or AGENT_CONFIG_PATH
    try:
        config = yaml.safe_load(target.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        logger.warning("Agent config unreadable at %s: %s", target, exc)
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}

    system_prompt = (config.get("workflow") or {}).get("system_prompt")
    rails = (
        ((config.get("middleware") or {}).get("workflow_guardrails") or {}).get("guardrails")
        or {}
    ).get("prompts")
    return {
        "available": isinstance(system_prompt, str),
        "system_prompt": system_prompt,
        "rail_prompts": rails,
        "prompt_sha256": _sha256(system_prompt) if isinstance(system_prompt, str) else None,
        "guardrails_prompts_sha256": _canonical_sha256(rails) if rails else None,
    }


def _register_if_changed(name: str, template: str) -> dict[str, Any]:
    """Register a prompt version only when the text actually changed.

    ``register_prompt`` increments on every call, so registering unconditionally
    would mint a version per suite per run and make the version number a run
    counter rather than a description of the prompt.
    """
    try:
        current = mlflow.genai.load_prompt(f"prompts:/{name}@latest")
        if current.template == template:
            return {"name": name, "version": str(current.version), "registered": False}
    except Exception:  # noqa: BLE001 - absent, or first ever registration
        current = None

    try:
        created = mlflow.genai.register_prompt(
            name=name,
            template=template,
            commit_message="Recorded automatically by the evaluation harness.",
        )
        return {"name": name, "version": str(created.version), "registered": True}
    except Exception as exc:  # noqa: BLE001 - provenance must not fail a run
        logger.warning("Could not register prompt %s: %s", name, exc)
        return {"name": name, "error": f"{type(exc).__name__}: {exc}"}


def register_and_link_prompts(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Register the agent's prompts, then load them inside the active run.

    The load is what creates the link: MLflow records the loaded version against
    the enclosing run, so provenance is stored by MLflow rather than asserted by
    us in a tag.
    """
    if not config.get("available"):
        return []

    registered: list[dict[str, Any]] = []
    targets = [(PROMPT_NAME, config["system_prompt"])]
    if config.get("rail_prompts"):
        targets.append((
            RAIL_PROMPT_NAME,
            yaml.safe_dump(config["rail_prompts"], sort_keys=True, default_flow_style=False),
        ))

    for name, template in targets:
        entry = _register_if_changed(name, template)
        if "error" not in entry:
            try:
                mlflow.genai.load_prompt(f"prompts:/{name}/{entry['version']}")
                entry["linked"] = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not link prompt %s: %s", name, exc)
                entry["linked"] = False
        registered.append(entry)
    return registered


def consistency_checks(
    agent: dict[str, Any], harness: dict[str, Any], config: dict[str, Any]
) -> dict[str, bool]:
    """Compare the three identities.

    A pure function so the unit tests exercise the real comparison rather than a
    copy of it — a mirrored implementation would be free to drift from this one,
    which is the failure mode the whole provenance record exists to catch.

    A check is only recorded when both sides are actually known. Comparing
    against ``unknown`` would manufacture a verdict instead of admitting there
    was nothing to compare.
    """
    checks: dict[str, bool] = {}
    if agent.get("available") and config.get("available"):
        checks["prompt_matches_config"] = agent.get("prompt_sha256") == config.get("prompt_sha256")
        checks["guardrail_prompts_match_config"] = agent.get(
            "guardrails_prompts_sha256"
        ) == config.get("guardrails_prompts_sha256")
    agent_commit = agent.get("build_commit")
    harness_commit = harness.get("git_commit")
    if agent_commit not in (None, "", "unknown") and harness_commit not in (None, "", "unknown"):
        checks["agent_built_from_harness_commit"] = agent_commit == harness_commit
    return checks


def collect(config_path: Path | None = None) -> dict[str, Any]:
    """Assemble the provenance record for one evaluation run."""
    agent = agent_identity()
    harness = harness_identity()
    config = _config_prompts(config_path)
    prompts = register_and_link_prompts(config)
    checks = consistency_checks(agent, harness, config)

    return {
        "agent": {key: value for key, value in agent.items() if key != "error"}
        | ({"error": agent["error"]} if "error" in agent else {}),
        "harness": harness,
        "prompts": prompts,
        "checks": checks,
        # One field to read. False means at least one identity disagreed, so the
        # run measured something other than this commit's configuration.
        "consistent": all(checks.values()) if checks else False,
    }


def _tag_tri_state(value: Any) -> str:
    """Render an optional boolean as an MLflow tag without losing "unobserved"."""
    if value is None:
        return SOURCE_UNKNOWN
    return str(bool(value)).lower()


def mlflow_tags(record: dict[str, Any]) -> dict[str, str]:
    """Flatten provenance into run tags so MLflow can group and compare runs."""
    agent = record.get("agent", {})
    harness = record.get("harness", {})
    tags = {
        "provenance.agent.build_commit": str(agent.get("build_commit", "unknown")),
        "provenance.agent.model": str(agent.get("model", "")),
        "provenance.agent.guard_model": str(agent.get("guard_model", "")),
        "provenance.agent.prompt_sha256": str(agent.get("prompt_sha256") or ""),
        "provenance.agent.config_sha256": str(agent.get("config_sha256") or ""),
        "provenance.agent.tool_count": str(len(agent.get("tools_exposed") or [])),
        "provenance.harness.git_commit": str(harness.get("git_commit", "unknown")),
        # "unknown" rather than "false" when no git tree was observable, so a tag
        # search for dirty=false cannot silently include a tree nobody inspected.
        "provenance.harness.dirty": _tag_tri_state(harness.get("dirty")),
        "provenance.harness.source": str(harness.get("source", SOURCE_UNKNOWN)),
        "provenance.consistent": str(record.get("consistent", False)).lower(),
    }
    for entry in record.get("prompts", []):
        if "version" in entry:
            tags[f"provenance.prompt.{entry['name']}"] = f"v{entry['version']}"
    return tags


def active_model_name(record: dict[str, Any]) -> str:
    """Version identifier for ``mlflow.set_active_model``.

    Built from what the agent reports, so every trace links to the deployed
    build rather than to the host's working tree.
    """
    agent = record.get("agent", {})
    commit = str(agent.get("build_commit") or "unknown")[:8]
    prompt = str(agent.get("prompt_sha256") or "nopprompt")[:8]
    return f"etf-research-agent-{commit}-p{prompt}"
