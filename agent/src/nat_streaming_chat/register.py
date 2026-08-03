"""Lean guarded streaming chat workflow for NeMo Agent Toolkit.

The application model is called directly through the OpenAI Python client.
This intentionally avoids the broad ``nvidia-nat-langchain`` integration
package while preserving NeMo Agent Toolkit workflow and middleware support.
"""

from collections.abc import AsyncGenerator
from typing import Any
from typing import Literal
from typing import cast

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel
from pydantic import Field

from nat.builder.builder import Builder
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.api_server import ChatRequest
from nat.data_models.api_server import ChatRequestOrMessage
from nat.data_models.api_server import Message
from nat.data_models.api_server import UserMessageContentRoleType
from nat.data_models.function import FunctionBaseConfig
from nat.utils.type_converter import GlobalTypeConverter


class GuardedChatInput(BaseModel):
    """Chat history plus one top-level string that middleware can guard."""

    user_input: str = Field(description="Latest user message guarded at pre-invoke")
    messages: list[Message] = Field(description="OpenAI-compatible conversation history")

    def __str__(self) -> str:
        # GuardrailsMiddleware uses str(input) as output-rail user context.
        return self.user_input


class StreamingChatCompletionConfig(FunctionBaseConfig, name="streaming_chat_completion"):
    """Connection and generation settings for the direct Ollama workflow."""

    base_url: str = Field(description="OpenAI-compatible API base URL")
    api_key: str = Field(default="ollama", description="API key accepted by the endpoint")
    model_name: str = Field(description="Model name sent to the OpenAI-compatible endpoint")
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, gt=0)
    request_timeout: float = Field(default=300.0, gt=0.0)
    reasoning_effort: Literal["none", "low", "medium", "high"] = Field(
        default="none",
        description="Ollama OpenAI-compatible reasoning level; none disables Qwen3 thinking",
    )
    system_prompt: str = Field(
        default="You are a helpful AI assistant.",
        description="System message prepended when the request has none",
    )
    description: str = Field(
        default="Guardrails-compatible direct OpenAI streaming workflow",
        description="Workflow description",
    )


def _content_to_text(content: Any) -> str:
    """Normalize NAT/OpenAI text content into one string for this POC."""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            else:
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return "" if content is None else str(content)


def _latest_user_text(messages: list[Message]) -> str:
    for message in reversed(messages):
        if message.role == UserMessageContentRoleType.USER:
            return _content_to_text(message.content)
    return ""


def _chat_request_to_guarded_input(request: ChatRequest) -> GuardedChatInput:
    return GuardedChatInput(
        user_input=_latest_user_text(request.messages),
        messages=request.messages,
    )


def _chat_request_or_message_to_guarded_input(request: ChatRequestOrMessage) -> GuardedChatInput:
    chat_request = GlobalTypeConverter.get().convert(request, to_type=ChatRequest)
    return _chat_request_to_guarded_input(chat_request)


def _string_to_guarded_input(message: str) -> GuardedChatInput:
    return _chat_request_to_guarded_input(ChatRequest.from_string(message))


# Allow NAT's OpenAI-compatible endpoint to feed the middleware-friendly input
# model without discarding the complete message history.
GlobalTypeConverter.register_converter(_chat_request_to_guarded_input)
GlobalTypeConverter.register_converter(_chat_request_or_message_to_guarded_input)
GlobalTypeConverter.register_converter(_string_to_guarded_input)


def _to_openai_messages(
    chat_input: GuardedChatInput,
    system_prompt: str,
) -> list[ChatCompletionMessageParam]:
    messages: list[dict[str, Any]] = []

    for message in chat_input.messages:
        # NAT's base Message model only guarantees ``role`` and ``content``.
        # Tool-related attributes are present only on some message variants,
        # so accessing ``message.tool_calls`` directly raises AttributeError
        # for ordinary user/system messages. Pydantic's JSON dump includes
        # only fields supported by the concrete message model.
        raw_message = message.model_dump(mode="json", exclude_none=True)
        role = raw_message.get("role", "user")

        item: dict[str, Any] = {
            "role": role,
            "content": _content_to_text(raw_message.get("content", "")),
        }

        # Preserve OpenAI tool/name metadata when a richer NAT message model
        # actually provides it, without requiring those optional attributes.
        for optional_field in ("name", "tool_calls", "tool_call_id"):
            if optional_field in raw_message:
                item[optional_field] = raw_message[optional_field]

        messages.append(item)

    # Input rails can rewrite user_input. Replace the latest user message with
    # the approved value before calling the application model.
    for message in reversed(messages):
        if message.get("role") == UserMessageContentRoleType.USER.value:
            message["content"] = chat_input.user_input
            break

    has_system_message = any(message.get("role") == "system" for message in messages)
    if system_prompt and not has_system_message:
        messages.insert(0, {"role": "system", "content": system_prompt})

    return cast(list[ChatCompletionMessageParam], messages)


@register_function(config_type=StreamingChatCompletionConfig)
async def register_streaming_chat_completion(
    config: StreamingChatCompletionConfig,
    _builder: Builder,
):
    client = AsyncOpenAI(
        base_url=config.base_url,
        api_key=config.api_key,
        timeout=config.request_timeout,
    )

    async def _response_fn(chat_input: GuardedChatInput) -> str:
        response = await client.chat.completions.create(
            model=config.model_name,
            messages=_to_openai_messages(chat_input, config.system_prompt),
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            stream=False,
            extra_body={"reasoning_effort": config.reasoning_effort},
        )
        if not response.choices:
            return ""
        return response.choices[0].message.content or ""

    async def _stream_fn(chat_input: GuardedChatInput) -> AsyncGenerator[str]:
        stream = await client.chat.completions.create(
            model=config.model_name,
            messages=_to_openai_messages(chat_input, config.system_prompt),
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            stream=True,
            extra_body={"reasoning_effort": config.reasoning_effort},
        )

        # The Guardrails streaming middleware receives plain strings. NAT's
        # OpenAI-compatible endpoint converts approved strings to response chunks.
        async for event in stream:
            if not event.choices:
                continue
            text = event.choices[0].delta.content
            if text:
                yield text

    try:
        yield FunctionInfo.create(
            single_fn=_response_fn,
            stream_fn=_stream_fn,
            description=config.description,
        )
    finally:
        await client.close()
