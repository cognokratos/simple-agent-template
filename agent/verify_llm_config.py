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

The second half asserts what the provider exists for, against the *built*
client and in both directions: an optional pass-through parameter configured
empty must be **absent**, not sent as ``""`` or as the literal string
``"null"``; one configured with a value — ``none`` included — must arrive
verbatim.

Each scenario pins its own input. The shipped ``.env`` sets
``LLM_REASONING_EFFORT=none``, so a check that built from the ambient
environment would assert whatever the operator configured rather than the
provider's behaviour — and an earlier version did exactly that, failing on the
repository's own default while claiming to test the empty case.

Note for upgrades: NAT 1.9 wraps every LangChain client in
``configurable_fields(model_name=...)``, so ``builder.get_llm`` returns a
``RunnableConfigurableFields`` where 1.8 returned a ``ChatOpenAI``. The checks
below unwrap it rather than assuming the shape — and assert that the wrapper is
there, because its presence is what makes per-request model selection possible
upstream, and this workflow deliberately does not offer it.

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


def _unwrap_chat_client(client):
    """Reach the ChatOpenAI underneath NAT's Runnable wrappers.

    NAT 1.9 wraps every LangChain client in
    ``configurable_fields(model_name=...)`` so a request can override the model
    per call, which makes the object NAT hands back a
    ``RunnableConfigurableFields`` rather than the ``ChatOpenAI`` 1.8 returned.
    The wrapper keeps the real client on ``.default``; retry and thinking
    patches may add further layers, so unwrap until there is nothing left to
    unwrap rather than assuming a fixed depth.
    """

    seen = 0
    while (inner := getattr(client, "default", None)) is not None and seen < 8:
        client = inner
        seen += 1
    return client


#: Stands for "variable not set" in ``_load_primary``.
UNSET = object()

#: The variable ``config.yml`` interpolates into ``primary.reasoning_effort``.
REASONING_EFFORT_ENV = "LLM_REASONING_EFFORT"


def _load_primary(reasoning_effort=UNSET) -> OptionalParamsOpenAIModelConfig:
    """Load the shipped ``primary`` LLM with ``LLM_REASONING_EFFORT`` pinned.

    Pinned, never inherited: see the module docstring. The caller's value is
    restored afterwards, so scenarios cannot leak into one another.
    """

    previous = os.environ.get(REASONING_EFFORT_ENV)
    if reasoning_effort is UNSET:
        os.environ.pop(REASONING_EFFORT_ENV, None)
    else:
        os.environ[REASONING_EFFORT_ENV] = reasoning_effort
    try:
        return load_config(str(CONFIG_PATH)).llms["primary"]
    finally:
        if previous is None:
            os.environ.pop(REASONING_EFFORT_ENV, None)
        else:
            os.environ[REASONING_EFFORT_ENV] = previous


def _primary_with(**overrides) -> OptionalParamsOpenAIModelConfig:
    """The shipped ``primary`` LLM with ``reasoning_effort`` set in the config itself.

    Covers what environment interpolation cannot produce: a literal empty or
    blank string, an explicit ``None``, and a key that is not written at all.
    Re-validated, so the provider's own pruning runs exactly as it does on a
    loaded config.
    """

    base = _load_primary().model_dump(exclude_unset=True, exclude={"reasoning_effort"})
    return OptionalParamsOpenAIModelConfig.model_validate({**base, **overrides})


async def _build(llm_config):
    async with WorkflowBuilder() as builder:
        await builder.add_llm("primary", llm_config)
        return await builder.get_llm("primary", wrapper_type=LLMFrameworkEnum.LANGCHAIN)


def _sent_reasoning_effort(chat_client) -> object:
    """What ChatOpenAI will actually send, wherever it chose to keep it."""

    value = getattr(chat_client, "reasoning_effort", None)
    if value is not None:
        return value
    return (getattr(chat_client, "model_kwargs", None) or {}).get("reasoning_effort")


async def check_the_client_actually_builds() -> None:
    """The regression. Needs no network: ChatOpenAI construction is local."""

    os.environ.setdefault("MCP_API_KEY", "verify-only-not-used")
    client = await _build(_load_primary(""))

    assert client is not None, "the provider yielded no client"

    chat_client = _unwrap_chat_client(client)
    assert type(chat_client).__name__ == "ChatOpenAI", (
        f"expected a ChatOpenAI under NAT's wrappers, found {type(chat_client).__name__} "
        f"(outermost was {type(client).__name__})"
    )
    print(f"PASS: the registered provider builds a {type(chat_client).__name__}")

    model = getattr(chat_client, "model_name", None)
    assert model, "the built client has no model name"
    print(f"PASS: the built client is bound to {model!r}")

    # The point of the provider, asserted against the *built* object because
    # the dump-level checks above cannot see what ChatOpenAI actually received.
    # Both directions, so neither can pass by the provider always dropping the
    # parameter, or always sending it.
    for label, llm_config in (
        ("LLM_REASONING_EFFORT=''", _load_primary("")),
        ("LLM_REASONING_EFFORT='   '", _load_primary("   ")),
        ("LLM_REASONING_EFFORT unset", _load_primary(UNSET)),
        # YAML turns an empty interpolation into null before the provider sees
        # it, so only these two hand the provider a literal empty string.
        ("reasoning_effort: ''", _primary_with(reasoning_effort="")),
        ("reasoning_effort: '   '", _primary_with(reasoning_effort="   ")),
        ("reasoning_effort: null", _primary_with(reasoning_effort=None)),
        ("reasoning_effort omitted", _primary_with()),
    ):
        sent = _sent_reasoning_effort(_unwrap_chat_client(await _build(llm_config)))
        assert sent is None, f"{label}: reasoning_effort reached the built client as {sent!r}"
    print("PASS: an empty, blank, null or unset optional parameter is absent from the built client")

    # `none` first: it is the shipped default for Qwen3 on Ollama, and the
    # value most easily mistaken for an absence.
    for value in ("none", "low", "medium", "high"):
        sent = _sent_reasoning_effort(_unwrap_chat_client(await _build(_load_primary(value))))
        assert sent == value, f"LLM_REASONING_EFFORT={value!r} reached the built client as {sent!r}"
    print("PASS: a configured optional parameter reaches the built client verbatim ('none' included)")

    # NAT 1.9 added per-request model override. This workflow does not use it —
    # `register._stream_fn` never passes `configurable` — and the gateway
    # rejects a client-supplied `model` outright (`deny_unknown_fields` on
    # ChatProxyRequest), so the configured model is the only one reachable.
    # Asserted so that adopting NAT's own `_build_lc_config` later is a visible
    # decision rather than a silent handover of model choice to the caller.
    assert type(client).__name__ == "RunnableConfigurableFields", (
        f"NAT no longer wraps the client for per-request model override "
        f"(found {type(client).__name__}); re-check who can choose the model"
    )
    print("PASS: per-request model override exists upstream and is deliberately unused here")


def main() -> None:
    check_optional_parameters()
    asyncio.run(check_the_client_actually_builds())


if __name__ == "__main__":
    main()
