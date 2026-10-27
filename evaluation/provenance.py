"""Link every evaluation run to the agent and prompt version that produced it.

Without this, a result file records how a number was produced and nothing about
what was measured. ``tool_call_correct = 1.0`` names its dataset but not the agent,
so the number cannot be attributed to a prompt, a model or a build afterwards.

Three identities are collected, from three different places, because no single
place knows all of them:

``agent``
    Reported by the running container over its authenticated ``GET /version``.
    This is the only source that describes what actually answered the questions.
``prompts``
    Digested from ``agent/config.yml``, mounted read-only. The digest is what the
    running agent reports for the prompt it *loaded*, so the two can be compared
    without any registry. The optional MLflow reporter additionally registers
    the texts in MLflow's prompt registry and links the version to its run; that
    version is reporting metadata, recorded beside the result, never part of it.
``harness``
    The source commit of this repository, passed in by the Makefile. The evaluator
    container has no ``.git`` and no working tree, so ``get_git_commit(".")``
    inside it would return ``None``. The host is the only place that can answer.

    ``source`` records *where* that answer came from:

    ``git``
        a real checkout; ``dirty`` was observed and is a true boolean.
    ``unknown``
        no git tree was available — an exported tarball, a container build
        context — so the commit is honestly ``unknown``.

    ``dirty`` is therefore ``bool | None``, and ``None`` means "not observable"
    rather than "clean". Equating those two would let a tree nobody inspected
    claim a verified clean checkout.

The interesting field is ``consistent``. The evaluator digests the prompt from
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


logger = logging.getLogger(__name__)

#: Mounted read-only by the evaluator service.
AGENT_CONFIG_PATH = Path(os.environ.get("AGENT_CONFIG_PATH", "/app/agent-config.yml"))

PROMPT_NAME = os.getenv("EVALUATION_SYSTEM_PROMPT_NAME", "agent-system-prompt")
RAIL_PROMPT_NAME = os.getenv("EVALUATION_RAIL_PROMPT_NAME", "agent-guardrail-input-rail")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_sha256(value: Any) -> str:
    """Mirror of the retired NAT agent's ``canonical_sha256`` (``nat-1.9-final``).

    Used only to digest that agent's rail-prompt layout, so the harness can
    still evaluate the preserved NAT agent for a framework comparison; the
    current Rust agent is digested with ``_canonical_json_sha256`` below.
    Duplicated rather than imported: the evaluator never installs an agent
    package.

    The duplication is not taken on trust. Both sides digest the same file, so
    every run compares the two results and reports ``prompt_matches_config`` /
    ``guardrail_prompts_match_config``; if either helper drifts, or the two
    containers resolve a different PyYAML, the next run says so instead of
    quietly agreeing. ``make eval-test`` additionally pins this helper's output
    for a fixed fragment so an accidental edit here fails offline.
    """
    import yaml

    return _sha256(yaml.safe_dump(value, sort_keys=True, default_flow_style=False))


def _canonical_json_sha256(value: Any) -> str:
    """Digest of sorted-key, compact JSON — the form the Rust agent reports.

    The agent digests its rail prompts this way
    (``guardrails_prompts_digest: "canonical-json"``), because reproducing
    PyYAML's ``safe_dump`` byte for byte in Rust would be fragile. The YAML
    digest above is kept for the retired NAT runtime, so the same
    ``prompt_matches_config`` comparison holds against either.
    """
    return _sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


#: What ``agent_runtime`` is recorded as when a reachable agent does not say:
#: the retired NAT agent (tag ``nat-1.9-final``) predates the field. Kept so the
#: harness can still evaluate that agent for a framework comparison.
NAT_RUNTIME = "nat"


def agent_runtime(agent: dict[str, Any]) -> str:
    """Which implementation answered: ``rig-rust``, ``nat``, or ``unknown``."""
    runtime = agent.get("agent_runtime")
    if isinstance(runtime, str) and runtime.strip():
        return runtime.strip()
    return NAT_RUNTIME if agent.get("available") else "unknown"


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
        headers={
            "Authorization": f"Bearer {os.getenv('AGENT_API_KEY', '')}",
            # Every non-health route requires an asserted identity, not only the
            # workflow. Same synthetic principal the suites run under.
            "x-authenticated-user-id": os.getenv("EVALUATION_PRINCIPAL", "").strip() or "evaluation-harness",
        },
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
        # Imported here, so the rest of the evaluator (and its host-side tests)
        # needs nothing beyond the standard library.
        import yaml

        config = yaml.safe_load(target.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        logger.warning("Agent config unreadable at %s: %s", target, exc)
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}

    system_prompt = (config.get("workflow") or {}).get("system_prompt")
    # Two layouts: the Rust agent's (`guardrails.prompts`) and the retired NAT
    # runtime's (`middleware.workflow_guardrails.guardrails.prompts`, tag
    # nat-1.9-final), each digested the way its agent digests it.
    nat_rails = (
        ((config.get("middleware") or {}).get("workflow_guardrails") or {}).get("guardrails")
        or {}
    ).get("prompts")
    rust_rails = (config.get("guardrails") or {}).get("prompts") if not nat_rails else None
    rails = nat_rails or rust_rails
    if nat_rails:
        rails_digest = _canonical_sha256(nat_rails)
    elif rust_rails:
        rails_digest = _canonical_json_sha256(rust_rails)
    else:
        rails_digest = None
    return {
        "available": isinstance(system_prompt, str),
        "system_prompt": system_prompt,
        "rail_prompts": rails,
        "prompt_sha256": _sha256(system_prompt) if isinstance(system_prompt, str) else None,
        "guardrails_prompts_sha256": rails_digest,
    }


def prompt_digests(config: dict[str, Any]) -> list[dict[str, Any]]:
    """The prompts this run was configured with, identified by content.

    Backend-neutral on purpose: a digest needs no running service to compute and
    no registry to resolve, so the record is complete with every reporter off.
    """
    if not config.get("available"):
        return []
    prompts = [{"name": PROMPT_NAME, "sha256": config.get("prompt_sha256"), "source": "agent/config.yml"}]
    if config.get("rail_prompts"):
        prompts.append(
            {
                "name": RAIL_PROMPT_NAME,
                "sha256": config.get("guardrails_prompts_sha256"),
                "source": "agent/config.yml",
            }
        )
    return prompts


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
    prompts = prompt_digests(config)
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
    """Render an optional boolean as a string tag without losing "unobserved"."""
    if value is None:
        return SOURCE_UNKNOWN
    return str(bool(value)).lower()


def flat_tags(record: dict[str, Any]) -> dict[str, str]:
    """Flatten provenance into string tags, so any platform can group and filter runs.

    Phoenix receives these as experiment metadata and MLflow as run tags; both
    are views over the same record. An absent value is the empty string or
    ``unknown``, never a guess.
    """
    agent = record.get("agent", {})
    harness = record.get("harness", {})
    tags = {
        # Which implementation answered: `rig-rust`, or `nat` for the retired runtime.
        "provenance.agent.runtime": agent_runtime(agent),
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
        if entry.get("sha256"):
            tags[f"provenance.prompt.{entry['name']}"] = str(entry["sha256"])[:12]
    return tags


def _known(value: Any) -> Any:
    """``None`` for a value nobody reported, so absence stays visible."""
    if value in (None, "", "unknown"):
        return None
    return value


def comparison_conditions(
    record: dict[str, Any], *, dataset_sha256: str, required_metric: str, fail_threshold: float
) -> dict[str, Any]:
    """What must match for two results to measure the same thing.

    Equal ``sha256`` means the same cases, scorer code, gate, agent build, model
    and prompts. Unequal, diff ``fields`` to see which one moved. ``provider`` is
    ``None`` because the agent's ``/version`` does not report its endpoint; a
    field that was not observed is ``None`` rather than guessed, and two ``None``
    values are not evidence of a match.
    """
    agent = record.get("agent", {})
    harness = record.get("harness", {})
    fields = {
        "dataset_sha256": dataset_sha256,
        "required_metric": required_metric,
        "fail_threshold": fail_threshold,
        "harness_git_commit": _known(harness.get("git_commit")),
        "harness_dirty": harness.get("dirty"),
        "agent_runtime": _known(agent_runtime(agent)),
        "agent_build_commit": _known(agent.get("build_commit")),
        "provider": _known(agent.get("provider")),
        "model": _known(agent.get("model")),
        "guard_model": _known(agent.get("guard_model")),
        "reasoning_effort": agent.get("reasoning_effort") if agent.get("available") else None,
        "prompt_sha256": _known(agent.get("prompt_sha256")),
        "guardrails_prompts_sha256": _known(agent.get("guardrails_prompts_sha256")),
        "config_sha256": _known(agent.get("config_sha256")),
    }
    unobserved = sorted(name for name, value in fields.items() if value is None)
    return {
        "sha256": _sha256(json.dumps(fields, sort_keys=True, separators=(",", ":"))),
        "fields": fields,
        "unobserved": unobserved,
    }
