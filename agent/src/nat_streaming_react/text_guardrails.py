# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# SPDX-License-Identifier: Apache-2.0
"""Text-aware NeMo Guardrails middleware for NAT chat streams.

NAT 1.8's generic Guardrails middleware converts every streaming item with
``str(chunk)`` before giving it to NeMo Guardrails. For a ``ChatResponseChunk``
that serializes the complete Pydantic object instead of forwarding only the
assistant text. This subclass extracts ``delta.content``, runs the configured
streaming rails on clean text, and wraps sanitized text back into
``ChatResponseChunk`` objects for NAT's API serializer.
"""

import json
from collections.abc import AsyncGenerator
from collections.abc import AsyncIterator
from typing import Any

from nat.builder.builder import Builder
from nat.cli.register_workflow import register_middleware
from nat.data_models.api_server import ChatResponseChunk
from nat.middleware.function_middleware import CallNextStream
from nat.middleware.middleware import InvocationContext
from nat.plugins.security.middleware.guardrails.nemo_guardrails_middleware import GuardrailsMiddleware
from nat.plugins.security.middleware.guardrails.nemo_guardrails_middleware_config import GuardrailsMiddlewareConfig


class TextGuardrailsMiddlewareConfig(
    GuardrailsMiddlewareConfig,
    name="text_guardrails",
):
    """Guardrails configuration with text-aware chat-stream handling."""


def _content_from_chat_chunk(chunk: ChatResponseChunk) -> str:
    """Extract assistant text from all choices in one NAT chat chunk."""

    parts: list[str] = []
    for choice in chunk.choices:
        content = choice.delta.content
        if content:
            parts.append(content)
    return "".join(parts)


def _input_text(raw: Any) -> str:
    """Extract the latest user text without serializing an entire ChatRequest."""

    if isinstance(raw, str):
        return raw

    direct = getattr(raw, "input_message", None)
    if isinstance(direct, str) and direct:
        return direct

    messages = getattr(raw, "messages", None)
    if messages:
        for message in reversed(messages):
            content = getattr(message, "content", None)
            role = getattr(message, "role", None)
            role_value = getattr(role, "value", role)
            if role_value == "user" and isinstance(content, str):
                return content

    return ""


class TextGuardrailsMiddleware(GuardrailsMiddleware):
    """Apply NeMo rails to text while preserving NAT chat chunk types."""

    async def _stream_with_output_rails(
        self,
        ctx: InvocationContext,
        call_next: CallNextStream,
    ) -> AsyncIterator[Any]:
        """Sanitize clean text chunks and rewrap them for NAT's chat API."""

        await self.bind_llms_to_rail()

        input_text = _input_text(ctx.modified_args[0]) if ctx.modified_args else ""
        messages: list[dict[str, str]] = (
            [{"role": "user", "content": input_text}] if input_text else []
        )

        # The first upstream item establishes the stream's output shape. The
        # mutable state is populated before LLMRails can yield its first result.
        stream_state: dict[str, Any] = {
            "is_chat_chunk": False,
            "id": None,
            "created": None,
            "model": None,
            "system_fingerprint": None,
            "service_tier": None,
        }

        async def upstream_text() -> AsyncIterator[str]:
            async for item in call_next(*ctx.modified_args, **ctx.modified_kwargs):
                if isinstance(item, ChatResponseChunk):
                    if not stream_state["is_chat_chunk"]:
                        stream_state.update(
                            {
                                "is_chat_chunk": True,
                                "id": item.id,
                                "created": item.created,
                                "model": item.model,
                                "system_fingerprint": item.system_fingerprint,
                                "service_tier": item.service_tier,
                            }
                        )
                    text = _content_from_chat_chunk(item)
                    if text:
                        yield text
                    continue

                if isinstance(item, str):
                    if item:
                        yield item
                    continue

                # Preserve support for other stream implementations, but never
                # use this path for NAT's ChatResponseChunk workflow.
                text = str(item)
                if text:
                    yield text

        def wrap_text(text: str) -> Any:
            if not stream_state["is_chat_chunk"]:
                return text

            return ChatResponseChunk.create_streaming_chunk(
                text,
                id_=stream_state["id"],
                created=stream_state["created"],
                model=stream_state["model"],
                system_fingerprint=stream_state["system_fingerprint"],
            )

        async for guarded_text in self._llm_rails.stream_async(
            messages=messages,
            generator=upstream_text(),
        ):
            try:
                payload = json.loads(guarded_text)
                if isinstance(payload, dict) and "error" in payload:
                    error = payload.get("error") or {}
                    error_message = error.get(
                        "message",
                        "Blocked by output rail.",
                    )
                    ctx.output = ""
                    blocked = self.on_post_invoke_blocked(ctx, error_message)
                    if isinstance(blocked, str):
                        yield wrap_text(blocked)
                    else:
                        yield blocked
                    return
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

            if guarded_text:
                yield wrap_text(guarded_text)


@register_middleware(config_type=TextGuardrailsMiddlewareConfig)
async def text_guardrails_middleware(
    config: TextGuardrailsMiddlewareConfig,
    builder: Builder,
) -> AsyncGenerator[TextGuardrailsMiddleware, None]:
    """Register the text-aware Guardrails middleware component."""

    middleware = TextGuardrailsMiddleware(config=config, builder=builder)
    await middleware.bind_llms_to_rail()
    yield middleware
