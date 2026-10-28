"""Mirror the repository's prompts into Phoenix Prompt Management.

``agent/config.yml`` is canonical. Phoenix receives a copy of each
source-controlled prompt so it can be browsed, diffed and replayed beside the
traces that used it; nothing reads a prompt back from Phoenix, and a Phoenix
prompt or version id is reporting metadata (``reporting.phoenix.prompts``),
never provenance. The portable identifier is the repository SHA-256.

What is mirrored, and nothing else:

* the agent system prompt (``workflow.system_prompt``), as the *template* —
  ``{tools}``/``{tool_names}`` are F-string variables, filled at agent startup;
* each guardrail prompt (``guardrails.prompts[]``) as its own prompt, named
  after its ``task``, as the user message the classifier receives —
  ``{{ user_input }}`` is a Mustache variable.

No conversation, no model response, no rendered user input ever goes here:
these are prompt *definitions*. ``AGENT_TRACE_CAPTURE_MODEL_CONTENT`` is not
involved.

Safety rule, the same as the MLflow reporter's: a prompt is mirrored only when
the mounted ``agent/config.yml`` still digests to what the result's provenance
recorded, so Phoenix never shows a text that differs from what was evaluated.

Idempotency: each version carries ``metadata.version_key``, a digest of the
content SHA-256 together with the model and invocation settings Phoenix
stores on the version. An existing version with that key is reused; a changed
text — or a changed model or setting, since a Phoenix version records those
too — creates exactly one new version. Repeated evaluation runs create none.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from typing import Any, Callable

from evaluation import provenance

SOURCE = "agent/config.yml"
RAIL_PROMPT_PREFIX = "agent-guardrail-"

#: LLM_PROVIDER names Phoenix has a provider for. Anything else (or none) is
#: recorded as OPENAI, because the agent speaks the OpenAI-compatible protocol
#: whatever serves it; the real name stays in the version's metadata.
PHOENIX_PROVIDERS = {
    "openai": "OPENAI",
    "azure_openai": "AZURE_OPENAI",
    "anthropic": "ANTHROPIC",
    "google": "GOOGLE",
    "deepseek": "DEEPSEEK",
    "xai": "XAI",
    "ollama": "OLLAMA",
    "aws": "AWS",
    "cerebras": "CEREBRAS",
    "fireworks": "FIREWORKS",
    "groq": "GROQ",
    "moonshot": "MOONSHOT",
    "minimax": "MINIMAX",
    "perplexity": "PERPLEXITY",
    "together": "TOGETHER",
    "zai": "ZAI",
    "meta": "META",
}
REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}

Request = Callable[..., Any]


def identifier(name: str) -> str:
    """A Phoenix prompt identifier: ``^[a-z0-9]([_a-z0-9-]*[a-z0-9])?$``."""
    cleaned = re.sub(r"[^_a-z0-9-]+", "-", name.lower()).strip("-_")
    return cleaned or "prompt"


def _sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _invocation(temperature: Any, max_tokens: Any, reasoning_effort: Any) -> dict[str, Any]:
    openai: dict[str, Any] = {}
    if isinstance(temperature, (int, float)):
        openai["temperature"] = float(temperature)
    if isinstance(max_tokens, int):
        openai["max_tokens"] = max_tokens
    if isinstance(reasoning_effort, str) and reasoning_effort in REASONING_EFFORTS:
        openai["reasoning_effort"] = reasoning_effort
    return {"type": "openai", "openai": openai}


def specs(result: dict[str, Any], config: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(prompts to mirror, prompts skipped with the reason), for one result."""
    record = result.get("provenance") or {}
    agent = record.get("agent") or {}
    expected = {entry.get("name"): entry for entry in record.get("prompts") or []}
    provider = agent.get("provider")
    model_provider = PHOENIX_PROVIDERS.get(str(provider or "").lower(), "OPENAI")
    common = {
        "source": SOURCE,
        "managed_by": "evaluation (repository mirror)",
        "provider": provider,
        "endpoint_sha256": agent.get("endpoint_sha256"),
        "agent_revision_sha256": agent.get("agent_revision_sha256"),
    }
    chosen: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    system = expected.get(provenance.PROMPT_NAME)
    if system:
        if not config.get("available") or system.get("sha256") != config.get("prompt_sha256"):
            skipped.append({"name": provenance.PROMPT_NAME, "reason": "the mounted config no longer matches the evaluated digest"})
        else:
            chosen.append(
                {
                    "name": identifier(provenance.PROMPT_NAME),
                    "description": f"Agent system prompt (workflow.system_prompt in {SOURCE}). The repository is canonical.",
                    "sha256": system["sha256"],
                    "role": "system",
                    "template_format": "F_STRING",
                    "content": config["system_prompt"],
                    "model_provider": model_provider,
                    "model_name": str(agent.get("model") or "unknown"),
                    "invocation_parameters": _invocation(
                        config.get("temperature"), config.get("max_tokens"), agent.get("reasoning_effort")
                    ),
                    "metadata": common
                    | {
                        "sha256": system["sha256"],
                        "source_key": "workflow.system_prompt",
                        "kind": "agent_system_prompt",
                        "model": agent.get("model"),
                        "rendered_prompt_sha256": agent.get("rendered_prompt_sha256"),
                        "tool_catalog_sha256": agent.get("tool_catalog_sha256"),
                        "variables": ["tools", "tool_names"],
                    },
                }
            )

    rails = expected.get(provenance.RAIL_PROMPT_NAME)
    if rails:
        if not config.get("available") or rails.get("sha256") != config.get("guardrails_prompts_sha256"):
            skipped.append({"name": provenance.RAIL_PROMPT_NAME, "reason": "the mounted config no longer matches the evaluated digest"})
        else:
            evaluated_tasks = {item["task"]: item["sha256"] for item in rails.get("tasks") or []}
            for entry in config.get("rail_prompts") or []:
                task = str(entry.get("task") or "")
                digest = (config.get("rail_prompt_sha256") or {}).get(task)
                if not task or not digest or evaluated_tasks.get(task, digest) != digest:
                    skipped.append({"name": f"{RAIL_PROMPT_PREFIX}{task}", "reason": "no matching per-task digest"})
                    continue
                chosen.append(
                    {
                        "name": identifier(f"{RAIL_PROMPT_PREFIX}{task}"),
                        "description": f"Guardrail prompt `{task}` (guardrails.prompts in {SOURCE}). The repository is canonical.",
                        "sha256": digest,
                        # The classifier receives the rendered prompt as a single
                        # user message (agent/src/agent/model.rs).
                        "role": "user",
                        "template_format": "MUSTACHE",
                        "content": str(entry.get("content") or ""),
                        "model_provider": model_provider,
                        "model_name": str(agent.get("guard_model") or "unknown"),
                        "invocation_parameters": _invocation(
                            agent.get("guard_temperature"), entry.get("max_tokens"), agent.get("guard_reasoning_effort")
                        ),
                        "metadata": common
                        | {
                            "sha256": digest,
                            "source_key": f"guardrails.prompts[task={task}]",
                            "kind": "guardrail_prompt",
                            "task": task,
                            "guard_model": agent.get("guard_model"),
                            "guardrails_prompts_sha256": rails.get("sha256"),
                        },
                    }
                )
    for spec in chosen:
        spec["metadata"]["version_key"] = _sha256(
            {
                "sha256": spec["sha256"],
                "role": spec["role"],
                "template_format": spec["template_format"],
                "model_provider": spec["model_provider"],
                "model_name": spec["model_name"],
                "invocation_parameters": spec["invocation_parameters"],
            }
        )
    return chosen, skipped


def _versions(request: Request, name: str) -> list[dict[str, Any]]:
    versions: list[dict[str, Any]] = []
    cursor = None
    while True:
        query: dict[str, Any] = {"limit": 100}
        if cursor:
            query["cursor"] = cursor
        page = request("GET", f"/v1/prompts/{urllib.parse.quote(name)}/versions", query=query) or {}
        versions.extend(page.get("data") or [])
        cursor = page.get("next_cursor")
        if not cursor:
            return versions


def _prompt_ids(request: Request) -> dict[str, str]:
    ids: dict[str, str] = {}
    cursor = None
    while True:
        query: dict[str, Any] = {"limit": 100}
        if cursor:
            query["cursor"] = cursor
        page = request("GET", "/v1/prompts", query=query) or {}
        ids.update({item["name"]: item["id"] for item in page.get("data") or []})
        cursor = page.get("next_cursor")
        if not cursor:
            return ids


def sync(request: Request, result: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Mirror the result's prompts; return ``{name: {status, version_id, ...}}``."""
    chosen, skipped = specs(result, config if config is not None else provenance._config_prompts())  # noqa: SLF001
    synced: dict[str, Any] = {}
    for spec in chosen:
        key = spec["metadata"]["version_key"]
        existing = next(
            (version for version in _versions(request, spec["name"]) if (version.get("metadata") or {}).get("version_key") == key),
            None,
        )
        if existing:
            synced[spec["name"]] = {"status": "reused", "version_id": existing["id"], "sha256": spec["sha256"]}
            continue
        created = request(
            "POST",
            "/v1/prompts",
            {
                # Phoenix appends a version when the prompt already exists.
                "prompt": {
                    "name": spec["name"],
                    "description": spec["description"],
                    "metadata": {"source": SOURCE, "managed_by": "evaluation (repository mirror)"},
                },
                "version": {
                    "description": f"{SOURCE} sha256:{spec['sha256'][:12]}",
                    "metadata": spec["metadata"],
                    "model_provider": spec["model_provider"],
                    "model_name": spec["model_name"],
                    "template": {"type": "chat", "messages": [{"role": spec["role"], "content": spec["content"]}]},
                    "template_type": "CHAT",
                    "template_format": spec["template_format"],
                    "invocation_parameters": spec["invocation_parameters"],
                },
            },
        )["data"]
        synced[spec["name"]] = {"status": "created", "version_id": created["id"], "sha256": spec["sha256"]}
    if synced:
        ids = _prompt_ids(request)
        for name, entry in synced.items():
            entry["prompt_id"] = ids.get(name)
    for item in skipped:
        synced[identifier(item["name"])] = {"status": "skipped", "reason": item["reason"]}
    return synced
