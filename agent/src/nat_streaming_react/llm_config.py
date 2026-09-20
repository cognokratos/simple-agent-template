# SPDX-License-Identifier: Apache-2.0
"""An LLM provider whose optional pass-through parameters can be *omitted*.

The problem
-----------
``reasoning_effort`` is not universally valid. Local Qwen3 through Ollama needs
``reasoning_effort: none`` — without it the model emits thinking text and the
short Yes/No guardrail classification breaks. On the real OpenAI API the
parameter applies only to reasoning models, and many OpenAI-compatible
endpoints reject it outright. So it must be *present* for one deployment and
*entirely absent* for another.

NAT's YAML interpolation cannot express absence. ``${VAR:-default}`` always
produces a string, for both an unset and an explicitly empty variable, and
``nat.llm.openai_llm.OpenAIModelConfig`` allows extra fields and dumps them
with ``exclude_unset=True`` — so any key written in the YAML is forwarded to the
client, whatever its value. Writing ``reasoning_effort: ${LLM_REASONING_EFFORT:-null}``
does not omit the parameter: it sends the four-character string ``"null"``, which
is worse than sending nothing, because it is a value the provider must reject.

Measured, not assumed::

    explicit 'null'  -> reasoning_effort in client kwargs: True   value='null'
    explicit ''      -> reasoning_effort in client kwargs: True   value=''
    explicit 'none'  -> reasoning_effort in client kwargs: True   value='none'
    omitted          -> reasoning_effort in client kwargs: False

The fix
-------
This provider drops any *extra* (non-declared) parameter whose configured value
is an empty or whitespace-only string, before pydantic records it as set. The
YAML then says what it means::

    reasoning_effort: ${LLM_REASONING_EFFORT:-}

* ``LLM_REASONING_EFFORT=none``  -> sent as ``reasoning_effort="none"``
* ``LLM_REASONING_EFFORT=low``   -> sent as ``reasoning_effort="low"``
* ``LLM_REASONING_EFFORT=``      -> not sent at all
* variable unset                 -> not sent at all

Only extras are affected. Declared fields such as ``model_name`` keep NAT's own
validation, so an empty required value still fails loudly instead of vanishing.
Nested mappings are pruned too, which covers ``extra_body``.

Select it with ``_type: openai_optional_params`` in ``llms:``. Everything else
about the provider is NAT's ``openai`` provider, unchanged.
"""

import logging
from typing import Any

from nat.builder.builder import Builder
from nat.builder.framework_enum import LLMFrameworkEnum
from nat.cli.register_workflow import register_llm_client
from nat.cli.register_workflow import register_llm_provider
from nat.llm.openai_llm import OpenAIModelConfig
from nat.plugins.langchain.llm import openai_langchain
from pydantic import model_validator

logger = logging.getLogger(__name__)


def _is_absent(value: Any) -> bool:
    """Whether a configured optional parameter should be treated as not set.

    ``None`` counts as absent as well as an empty string. NAT round-trips the
    config through YAML after interpolation, so an interpolated-empty scalar
    arrives here as ``None`` rather than ``""`` — and for an optional
    pass-through parameter, ``null`` and empty mean the same thing: omit it.
    """

    return value is None or (isinstance(value, str) and not value.strip())


def prune_empty_params(values: Any) -> Any:
    """Recursively drop mapping entries whose value is absent.

    Used for both the workflow LLM's extras and the guardrails model
    ``parameters``/``extra_body`` block, so "leave it empty to omit it" means the
    same thing everywhere in the configuration.
    """

    if isinstance(values, dict):
        pruned: dict[Any, Any] = {}
        for key, value in values.items():
            if _is_absent(value):
                continue
            cleaned = prune_empty_params(value)
            # An inner mapping that pruned down to nothing is dropped as well;
            # sending `extra_body: {}` is pointless and some clients reject it.
            if isinstance(value, dict) and not cleaned:
                continue
            pruned[key] = cleaned
        return pruned
    if isinstance(values, list):
        return [prune_empty_params(item) for item in values]
    return values


class OptionalParamsOpenAIModelConfig(
    OpenAIModelConfig,
    name="openai_optional_params",
):
    """OpenAI-compatible provider that omits empty optional parameters."""

    @model_validator(mode="before")
    @classmethod
    def _drop_empty_optional_parameters(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        declared = set(cls.model_fields)
        # Include validation aliases, so `model:` is not mistaken for an extra.
        for field in cls.model_fields.values():
            alias = getattr(field, "validation_alias", None)
            for choice in getattr(alias, "choices", ()) or ():
                if isinstance(choice, str):
                    declared.add(choice)

        cleaned: dict[Any, Any] = {}
        dropped: list[str] = []
        for key, value in data.items():
            if key in declared:
                cleaned[key] = value
                continue
            if _is_absent(value):
                dropped.append(str(key))
                continue
            pruned = prune_empty_params(value)
            if isinstance(value, dict) and not pruned:
                dropped.append(str(key))
                continue
            cleaned[key] = pruned

        if dropped:
            logger.info(
                "Omitting empty optional model parameter(s) from the request: %s",
                ", ".join(sorted(dropped)),
            )
        return cleaned


@register_llm_provider(config_type=OptionalParamsOpenAIModelConfig)
async def openai_optional_params_provider(
    config: OptionalParamsOpenAIModelConfig, _builder: Builder
):
    """Register the provider. The config class does all the work."""

    from nat.builder.llm import LLMProviderInfo

    yield LLMProviderInfo(
        config=config,
        description="OpenAI-compatible provider that omits empty optional parameters.",
    )


@register_llm_client(
    config_type=OptionalParamsOpenAIModelConfig,
    wrapper_type=LLMFrameworkEnum.LANGCHAIN,
)
async def openai_optional_params_langchain(
    llm_config: OptionalParamsOpenAIModelConfig, builder: Builder
):
    """Delegate to NAT's own OpenAI LangChain client builder.

    The subclass only changes which keys survive validation, so there is no
    reason to fork client construction: NAT's implementation is reused verbatim
    and stays correct across patch releases.

    ``async with``, not ``async for``: ``register_llm_client`` wraps the function
    it decorates in ``asynccontextmanager``, so the module-level
    ``openai_langchain`` name is a context-manager factory rather than an async
    generator. Iterating it raises ``TypeError: 'async for' requires an object
    with __aiter__`` at workflow-build time — which is how this was found, on a
    live start rather than in any offline check.
    """

    async with openai_langchain(llm_config, builder) as client:
        yield client
