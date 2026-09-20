"""Offline checks for the LLM provider and its optional-parameter handling.

Why this exists
---------------
The first version of ``openai_optional_params_langchain`` delegated with
``async for``. ``register_llm_client`` wraps the function it decorates in
``asynccontextmanager``, so the module-level ``openai_langchain`` name is a
context-manager factory rather than an async generator, and iterating it raises
``TypeError: 'async for' requires an object with __aiter__`` — at workflow-build
time, on a live start, well after every offline check had passed.

Building the client needs no network: ``ChatOpenAI`` construction does not call
the endpoint. So the failure was catchable offline and now is.

The second half asserts what the provider exists for: an optional pass-through
parameter configured empty must be **absent** from the client, not sent as ``""``
or as the literal string ``"null"``.

Run inside the agent image::

    docker compose exec agent python /app/verify_llm_config.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from nat.builder.framework_enum import LLMFrameworkEnum
from nat.builder.workflow_builder import WorkflowBuilder
from nat.runtime.loader import load_config

import nat_streaming_react.register  # noqa: F401  (registers the components)
from nat_streaming_react.llm_config import OptionalParamsOpenAIModelConfig, prune_empty_params

CONFIG_PATH = Path(__file__).with_name("config.yml")

#: Mirrors the exclusions NAT's own OpenAI client builder applies before handing
#: the dump to ChatOpenAI.
CLIENT_DUMP = dict(
    exclude={"type", "thinking", "api_type", "api_key", "base_url", "verify_ssl"},
    by_alias=True,
    exclude_none=True,
    exclude_unset=True,
)


def client_kwargs(**settings) -> dict:
    config = OptionalParamsOpenAIModelConfig(model_name="test-model", **settings)
    return config.model_dump(**CLIENT_DUMP)


def check_optional_parameters() -> None:
    """Empty means omitted; anything else is passed through verbatim."""

    for label, settings in (
        ("empty string", {"reasoning_effort": ""}),
        ("whitespace", {"reasoning_effort": "   "}),
        ("null", {"reasoning_effort": None}),
        ("omitted", {}),
    ):
        kwargs = client_kwargs(**settings)
        assert "reasoning_effort" not in kwargs, (
            f"{label}: reasoning_effort reached the client as "
            f"{kwargs['reasoning_effort']!r}; it must be omitted entirely"
        )
    print("PASS: an empty optional parameter is omitted from the request")

    for value in ("none", "low", "medium", "high"):
        kwargs = client_kwargs(reasoning_effort=value)
        assert kwargs.get("reasoning_effort") == value, kwargs
    print("PASS: a configured optional parameter is passed through verbatim")

    # `none` is a meaningful value for Qwen3 through Ollama, so it must NOT be
    # treated as absent. This is the one that makes the rule non-obvious.
    assert client_kwargs(reasoning_effort="none")["reasoning_effort"] == "none"
    print("PASS: 'none' is a value, not an absence")

    nested = client_kwargs(extra_body={"reasoning_effort": "", "keep": "yes"})
    assert nested.get("extra_body") == {"keep": "yes"}, nested
    assert "extra_body" not in client_kwargs(extra_body={"reasoning_effort": ""})
    print("PASS: nested empty parameters are pruned, and an emptied mapping is dropped")

    # Declared fields keep NAT's own validation rather than silently vanishing.
    assert client_kwargs()["model"] == "test-model"
    assert OptionalParamsOpenAIModelConfig(model="aliased").model_name == "aliased"
    print("PASS: declared fields and their aliases are untouched")

    assert prune_empty_params({"a": "", "b": None, "c": "x", "d": {"e": ""}}) == {"c": "x"}
    print("PASS: prune_empty_params drops absent values at every depth")


async def check_the_client_actually_builds() -> None:
    """The regression. Needs no network: ChatOpenAI construction is local."""

    os.environ.setdefault("MCP_API_KEY", "verify-only-not-used")
    config = load_config(str(CONFIG_PATH))

    async with WorkflowBuilder() as builder:
        await builder.add_llm("primary", config.llms["primary"])
        client = await builder.get_llm("primary", wrapper_type=LLMFrameworkEnum.LANGCHAIN)

    assert client is not None, "the provider yielded no client"
    assert type(client).__name__ == "ChatOpenAI", type(client).__name__
    print(f"PASS: the registered provider builds a {type(client).__name__}")

    model = getattr(client, "model_name", None)
    assert model, "the built client has no model name"
    print(f"PASS: the built client is bound to {model!r}")


def main() -> None:
    check_optional_parameters()
    asyncio.run(check_the_client_actually_builds())


if __name__ == "__main__":
    main()
