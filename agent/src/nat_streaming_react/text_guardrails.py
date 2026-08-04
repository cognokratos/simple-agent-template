# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
# SPDX-License-Identifier: Apache-2.0
"""Text-aware and observable NeMo Guardrails middleware for NAT chat streams.

NAT 1.8's generic Guardrails middleware converts every streaming item with
``str(chunk)`` before giving it to NeMo Guardrails. For a ``ChatResponseChunk``
that serializes the complete Pydantic object instead of forwarding only the
assistant text. This subclass extracts ``delta.content``, runs the configured
rails on clean text, wraps sanitized text back into ``ChatResponseChunk``
objects, and emits explicit OpenTelemetry decision spans into NAT's canonical
workflow trace.
"""

import hashlib
import json
import os
import re
from collections.abc import AsyncGenerator
from collections.abc import AsyncIterator
from typing import Any

from nemoguardrails.llm.types import Task
from nemoguardrails.rails.llm.options import GenerationLogOptions
from nemoguardrails.rails.llm.options import GenerationOptions
from nemoguardrails.rails.llm.options import GenerationResponse
from opentelemetry import trace
from opentelemetry.trace import Status
from opentelemetry.trace import StatusCode

from nat.builder.builder import Builder
from nat.cli.register_workflow import register_middleware
from nat.data_models.api_server import ChatResponseChunk
from nat.middleware.function_middleware import CallNextStream
from nat.middleware.middleware import InvocationContext
from nat.plugins.security.middleware.guardrails.nemo_guardrails_middleware import GuardrailsMiddleware
from nat.plugins.security.middleware.guardrails.nemo_guardrails_middleware_config import GuardrailsMiddlewareConfig

tracer = trace.get_tracer("nat_streaming_react.guardrails", "0.1.8")


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


def _content_to_text(content: Any) -> str:
    """Normalize NAT text or multimodal message content into plain text."""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                text = item.get("text")
            else:
                text = getattr(item, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)
    return ""


def _input_text(raw: Any) -> str:
    """Extract exactly the latest user message from a NAT chat request."""

    if isinstance(raw, str):
        return raw

    direct = getattr(raw, "input_message", None)
    if isinstance(direct, str) and direct:
        return direct

    messages = getattr(raw, "messages", None)
    if messages:
        for message in reversed(messages):
            role = getattr(message, "role", None)
            role_value = getattr(role, "value", role)
            if role_value == "user":
                return _content_to_text(getattr(message, "content", None))

    return ""


def _replace_latest_user_text(raw: Any, new_text: str) -> Any:
    """Return a copy of a NAT request with its latest user message replaced."""

    if isinstance(raw, str):
        return new_text

    direct = getattr(raw, "input_message", None)
    if direct is not None and hasattr(raw, "model_copy"):
        return raw.model_copy(update={"input_message": new_text})

    messages = getattr(raw, "messages", None)
    if messages and hasattr(raw, "model_copy"):
        updated_messages = list(messages)
        for index in range(len(updated_messages) - 1, -1, -1):
            message = updated_messages[index]
            role = getattr(message, "role", None)
            role_value = getattr(role, "value", role)
            if role_value != "user":
                continue
            if hasattr(message, "model_copy"):
                updated_messages[index] = message.model_copy(update={"content": new_text})
            elif isinstance(message, dict):
                updated_message = dict(message)
                updated_message["content"] = new_text
                updated_messages[index] = updated_message
            return raw.model_copy(update={"messages": updated_messages})

    return raw


_CRITICAL_INPUT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "prompt_injection",
        re.compile(
            r"\b(?:ignore|override|disregard|bypass)\b.{0,100}"
            r"\b(?:previous|prior|system|developer|hidden)\b.{0,80}"
            r"\b(?:instruction|instructions|prompt|message|rules?)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "system_prompt_or_tool_secret_extraction",
        re.compile(
            r"\b(?:reveal|show|print|dump|expose|list)\b.{0,100}"
            r"\b(?:system prompt|developer message|hidden instructions|"
            r"internal tool(?:ing)? (?:configuration|config)|tool configuration)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "criminal_financial_evasion",
        re.compile(
            r"\b(?:give|provide|write|show|tell|explain|help)\b.{0,80}"
            r"\b(?:step[- ]by[- ]step|instructions?|method|plan|how to)\b.{0,160}"
            r"\b(?:launder|hide|conceal|disguise|evade|avoid|bypass)\b.{0,160}"
            r"\b(?:criminal proceeds|illicit funds|money laundering|aml monitoring|"
            r"aml controls?|transaction monitoring|detection)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
)


def _critical_input_matches(text: str) -> list[str]:
    """Return deterministic high-confidence input-policy block matches."""

    if not _env_bool("GUARDRAILS_INPUT_DETERMINISTIC_FALLBACK", True):
        return []
    return [name for name, pattern in _CRITICAL_INPUT_PATTERNS if pattern.search(text)]


_ALERT_ID_PATTERN = r"ALT-[A-Z0-9][A-Z0-9_-]*"
_READ_ONLY_ALERT_TEMPLATES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "list_open_alerts",
        re.compile(
            r"^\s*(?:please\s+)?(?:show|list|display|get|retrieve)\s+"
            r"(?:me\s+)?(?:all\s+)?(?:of\s+)?(?:my\s+)?open\s+alerts?"
            r"(?:\s+(?:with|including)\s+(?:their\s+)?"
            r"(?:details?|status(?:es)?|severity|titles?))?[.!?]?\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "list_open_alert_transactions",
        re.compile(
            r"^\s*(?:please\s+)?(?:show|list|display|get|retrieve)\s+"
            r"(?:me\s+)?(?:all\s+)?transactions?\s+(?:for|from)\s+"
            r"(?:all\s+)?(?:my\s+)?open\s+alerts?"
            r"(?:\s+(?:with|including)\s+(?:their\s+)?details?)?[.!?]?\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "specific_alert_direct",
        re.compile(
            rf"^\s*(?:please\s+)?(?:show|display|get|retrieve|summarize)\s+"
            rf"(?:me\s+)?(?:alert\s+)?{_ALERT_ID_PATTERN}"
            r"(?:\s*,?\s*(?:and\s+)?(?:"
            r"quote\s+its\s+(?:complete\s+|full\s+)?description\s+exactly"
            r"(?:\s*,?\s*including\s+every\s+key\s+and\s+value)?|"
            r"including\s+(?:the\s+)?customer\s+and\s+assigned\s+analyst|"
            r"with\s+(?:all\s+)?transactions?))?[.!?]?\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "specific_alert_details",
        re.compile(
            rf"^\s*(?:please\s+)?(?:show|display|get|retrieve|summarize)\s+"
            r"(?:me\s+)?(?:the\s+)?(?:(?:complete|full)\s+)?"
            r"(?:details?|information)(?:\s+and\s+(?:all\s+)?transactions?)?\s+"
            rf"(?:for|of|from)\s+(?:alert\s+)?{_ALERT_ID_PATTERN}"
            r"(?:\s*,?\s*(?:including|with)\s+(?:the\s+)?(?:"
            r"customer\s+and\s+assigned\s+analyst|customer|assigned\s+analyst|"
            r"all\s+transactions?))?[.!?]?\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "specific_alert_transactions",
        re.compile(
            rf"^\s*(?:please\s+)?(?:show|list|display|get|retrieve)\s+"
            r"(?:me\s+)?(?:all\s+)?transactions?\s+(?:for|of|from)\s+"
            rf"(?:alert\s+)?{_ALERT_ID_PATTERN}[.!?]?\s*$",
            re.IGNORECASE,
        ),
    ),
)


def _read_only_alert_allow_matches(text: str) -> list[str]:
    """Recognize tightly scoped, read-only alert investigation requests.

    Every pattern is anchored to the complete message. This prevents an attacker
    from appending an instruction override or harmful request to an otherwise
    valid alert query and then benefiting from the allow override.
    """

    if not _env_bool("GUARDRAILS_INPUT_READ_ONLY_ALLOW_OVERRIDE", True):
        return []

    normalized = " ".join(text.split())
    if not normalized or len(normalized) > 500:
        return []

    return [
        name
        for name, pattern in _READ_ONLY_ALERT_TEMPLATES
        if pattern.fullmatch(normalized)
    ]


def _resolve_input_policy(
    *,
    llm_blocked: bool,
    deterministic_block_matches: list[str],
    deterministic_allow_matches: list[str],
) -> tuple[bool, bool, str]:
    """Resolve the final input decision and explain which layer decided it."""

    deterministic_blocked = bool(deterministic_block_matches)
    deterministic_allowed = bool(deterministic_allow_matches)
    allow_override_applied = (
        llm_blocked and deterministic_allowed and not deterministic_blocked
    )

    if deterministic_blocked:
        blocked = True
    elif deterministic_allowed:
        blocked = False
    else:
        blocked = llm_blocked

    if deterministic_blocked and llm_blocked:
        decision_source = "llm_and_deterministic_block"
    elif deterministic_blocked:
        decision_source = "deterministic_block_fallback"
    elif allow_override_applied:
        decision_source = "deterministic_allow_override"
    elif deterministic_allowed:
        decision_source = "llm_and_deterministic_allow"
    else:
        decision_source = "llm"

    return blocked, allow_override_applied, decision_source


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _trace_limit() -> int:
    try:
        return max(int(os.getenv("GUARDRAILS_TRACE_MAX_CHARS", "16384")), 256)
    except ValueError:
        return 16384


def _truncate(value: str) -> str:
    limit = _trace_limit()
    if len(value) <= limit:
        return value
    return value[:limit] + "\n[guardrail trace content truncated]"


def _normalize(value: Any) -> Any:
    """Convert Pydantic/dataclass-like values into JSON-compatible data."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        try:
            return _normalize(value.model_dump(mode="json", exclude_none=True))
        except TypeError:
            return _normalize(value.model_dump(exclude_none=True))
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_normalize(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            str(key): _normalize(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return str(value)


def _json(value: Any) -> str:
    return _truncate(json.dumps(_normalize(value), ensure_ascii=False, default=str))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _response_text(response: GenerationResponse) -> str:
    value = response.response
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for message in reversed(value):
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content
    return ""


def _response_log(response: GenerationResponse) -> Any:
    return _normalize(getattr(response, "log", None))


def _llm_calls_from_log(response: GenerationResponse) -> list[Any]:
    log = getattr(response, "log", None)
    calls = getattr(log, "llm_calls", None) if log is not None else None
    return list(calls or [])


def _activated_rails_from_log(response: GenerationResponse) -> list[Any]:
    log = getattr(response, "log", None)
    rails = getattr(log, "activated_rails", None) if log is not None else None
    return list(rails or [])


def _find_first(data: Any, candidate_keys: set[str]) -> Any:
    """Find the first matching field in a normalized nested log payload."""

    normalized = _normalize(data)
    if isinstance(normalized, dict):
        for key, value in normalized.items():
            if key.lower() in candidate_keys and value not in (None, "", [], {}):
                return value
        for value in normalized.values():
            found = _find_first(value, candidate_keys)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(normalized, list):
        for value in normalized:
            found = _find_first(value, candidate_keys)
            if found not in (None, "", [], {}):
                return found
    return None


def _rendered_self_check_prompt(llm_rails: Any, user_input: str) -> Any:
    """Render the configured self-check task exactly as Guardrails does."""

    manager = getattr(llm_rails, "llm_task_manager", None)
    if manager is None:
        return None
    try:
        return manager.render_task_prompt(
            Task.SELF_CHECK_INPUT,
            context={"user_input": user_input},
            force_string_to_message=True,
        )
    except Exception:
        # Prompt capture must never break the safety decision itself. The actual
        # LLM call log is still captured after generation when available.
        return None


def _set_common_guardrail_attributes(span: Any, *, stage: str, name: str) -> None:
    span.set_attribute("openinference.span.kind", "GUARDRAIL")
    span.set_attribute("guardrail.stage", stage)
    span.set_attribute("guardrail.name", name)
    span.set_attribute("guardrail.framework", "nemo-guardrails")
    span.set_attribute("guardrail.framework.version", "0.21.0")


class TextGuardrailsMiddleware(GuardrailsMiddleware):
    """Apply NeMo rails to text while preserving NAT chat chunk types."""

    async def pre_invoke(self, context: InvocationContext) -> InvocationContext | None:
        """Run input rails on the latest user message and record the verdict."""

        await self.bind_llms_to_rail()

        if not context.modified_args or context.modified_args[0] is None:
            return None

        value: Any = context.modified_args[0]
        text = _input_text(value)

        # NAT's generic selector only sees top-level string fields. ChatRequest
        # stores user text inside messages: list[Message], so relying on the
        # inherited default selector silently evaluates zero inputs. Explicitly
        # extract the latest user turn instead.
        if not text:
            with tracer.start_as_current_span("guardrail.input.self_check") as span:
                _set_common_guardrail_attributes(
                    span,
                    stage="input",
                    name="self check input",
                )
                span.set_attribute("guardrail.outcome", "skipped")
                span.set_attribute("guardrail.skipped", True)
                span.set_attribute("guardrail.skip_reason", "no_user_text_extracted")
                span.set_status(Status(StatusCode.OK))
            return None

        rendered_prompt = _rendered_self_check_prompt(self._llm_rails, text)
        deterministic_matches = _critical_input_matches(text)
        deterministic_allow_matches = _read_only_alert_allow_matches(text)

        with tracer.start_as_current_span("guardrail.input.self_check") as span:
            _set_common_guardrail_attributes(
                span,
                stage="input",
                name="self check input",
            )
            span.set_attribute("guardrail.type", "llm_self_check_with_deny_and_read_only_allow_overrides")
            span.set_attribute("gen_ai.operation.name", "guardrail_check")
            span.set_attribute("gen_ai.provider.name", "ollama")
            span.set_attribute(
                "gen_ai.request.model",
                os.getenv("OLLAMA_GUARD_MODEL", os.getenv("OLLAMA_MODEL", "qwen3:8b")),
            )
            span.set_attribute("guardrail.input.sha256", _sha256(text))
            span.set_attribute("guardrail.input.length", len(text))
            span.set_attribute(
                "guardrail.deterministic.matches",
                _json(deterministic_matches),
            )
            span.set_attribute(
                "guardrail.deterministic.blocked",
                bool(deterministic_matches),
            )
            span.set_attribute(
                "guardrail.deterministic.allow_matches",
                _json(deterministic_allow_matches),
            )
            span.set_attribute(
                "guardrail.deterministic.allow_candidate",
                bool(deterministic_allow_matches),
            )

            capture_content = _env_bool(
                "GUARDRAILS_TRACE_CAPTURE_CONTENT",
                True,
            )
            if capture_content:
                span.set_attribute("guardrail.user_input", _truncate(text))
                if rendered_prompt is not None:
                    span.set_attribute(
                        "guardrail.prompt.rendered",
                        _json(rendered_prompt),
                    )
                span.set_attribute(
                    "input.value",
                    _json(
                        {
                            "user_message": text,
                            "rendered_self_check_prompt": rendered_prompt,
                            "deterministic_block_matches": deterministic_matches,
                            "deterministic_allow_matches": deterministic_allow_matches,
                        }
                    ),
                )
                span.set_attribute("input.mime_type", "application/json")

            try:
                response: GenerationResponse = await self._llm_rails.generate_async(
                    messages=[{"role": "user", "content": text}],
                    options=GenerationOptions(
                        rails=["input"],
                        log=GenerationLogOptions(
                            activated_rails=True,
                            llm_calls=True,
                        ),
                        output_vars=["user_message", "bot_message"],
                    ),
                )

                llm_blocked = self._rail_blocked(response)

                # Decision precedence is intentionally asymmetric:
                # 1. Deterministic critical blocks always win.
                # 2. A narrow read-only alert allow rule can correct an LLM
                #    false positive, but only when no critical block matched.
                # 3. All other inputs follow the LLM self-check verdict.
                deterministic_blocked = bool(deterministic_matches)
                deterministic_allowed = bool(deterministic_allow_matches)
                blocked, allow_override_applied, decision_source = _resolve_input_policy(
                    llm_blocked=llm_blocked,
                    deterministic_block_matches=deterministic_matches,
                    deterministic_allow_matches=deterministic_allow_matches,
                )

                if deterministic_allowed and not deterministic_blocked:
                    # An allowed read-only query must be forwarded unchanged.
                    # A false-positive Guardrails response may contain a refusal;
                    # treating that refusal as a modified input would silently
                    # replace the user's valid request.
                    result_text = text
                else:
                    result_text = self._handle_modified_rail_response(
                        response,
                        fallback=text,
                    )
                modified = not blocked and result_text != text
                outcome = "blocked" if blocked else "modified" if modified else "passed"

                activated_rails = _activated_rails_from_log(response)
                llm_calls = _llm_calls_from_log(response)
                normalized_calls = _normalize(llm_calls)
                last_llm_call = llm_calls[-1] if llm_calls else None
                actual_prompt = (
                    getattr(last_llm_call, "prompt", None)
                    if last_llm_call is not None
                    else None
                )
                raw_completion = (
                    getattr(last_llm_call, "completion", None)
                    if last_llm_call is not None
                    else None
                )
                if actual_prompt in (None, "", [], {}):
                    actual_prompt = _find_first(
                        normalized_calls,
                        {"prompt", "messages", "input", "request"},
                    )
                if raw_completion in (None, "", [], {}):
                    raw_completion = _find_first(
                        normalized_calls,
                        {"completion", "response", "output", "result"},
                    )

                span.set_attribute("guardrail.outcome", outcome)
                span.set_attribute("guardrail.blocked", blocked)
                span.set_attribute("guardrail.modified", modified)
                span.set_attribute("guardrail.llm.blocked", llm_blocked)
                span.set_attribute("guardrail.final.blocked", blocked)
                span.set_attribute(
                    "guardrail.deterministic.allow_override_applied",
                    allow_override_applied,
                )
                span.set_attribute("guardrail.decision_source", decision_source)
                span.set_attribute(
                    "guardrail.activated_rails",
                    _json(activated_rails),
                )
                span.set_attribute("guardrail.llm_call_count", len(llm_calls))
                span.set_attribute("guardrail.llm.calls", _json(normalized_calls))
                span.set_attribute("guardrail.log", _json(_response_log(response)))
                if last_llm_call is not None:
                    for source_name, attribute_name in (
                        ("task", "guardrail.llm.task"),
                        ("llm_model_name", "guardrail.llm.model"),
                        ("llm_provider_name", "guardrail.llm.provider"),
                        ("duration", "guardrail.llm.duration_seconds"),
                        ("prompt_tokens", "guardrail.llm.prompt_tokens"),
                        ("completion_tokens", "guardrail.llm.completion_tokens"),
                        ("total_tokens", "guardrail.llm.total_tokens"),
                    ):
                        field_value = getattr(last_llm_call, source_name, None)
                        if field_value is not None:
                            span.set_attribute(attribute_name, field_value)

                if capture_content:
                    if actual_prompt is not None:
                        span.set_attribute(
                            "guardrail.llm.prompt",
                            _json(actual_prompt),
                        )
                    if raw_completion is not None:
                        span.set_attribute(
                            "guardrail.llm.response",
                            _json(raw_completion),
                        )
                    span.set_attribute(
                        "output.value",
                        _json(
                            {
                                "outcome": outcome,
                                "blocked": blocked,
                                "llm_blocked": llm_blocked,
                                "deterministic_block_matches": deterministic_matches,
                                "deterministic_allow_matches": deterministic_allow_matches,
                                "allow_override_applied": allow_override_applied,
                                "decision_source": decision_source,
                                "modified": modified,
                                "raw_guardrail_llm_response": raw_completion,
                                "activated_rails": activated_rails,
                                "llm_returned_message": _response_text(response),
                                "forwarded_user_message": result_text if not blocked else None,
                            }
                        ),
                    )
                    span.set_attribute("output.mime_type", "application/json")

                span.add_event(
                    "guardrail.decision",
                    {
                        "guardrail.name": "self check input",
                        "guardrail.outcome": outcome,
                        "guardrail.blocked": blocked,
                        "guardrail.llm.blocked": llm_blocked,
                        "guardrail.final.blocked": blocked,
                        "guardrail.deterministic.allow_override_applied": allow_override_applied,
                        "guardrail.decision_source": decision_source,
                        "guardrail.modified": modified,
                    },
                )
                span.set_status(Status(StatusCode.OK))

                if blocked:
                    if llm_blocked and not allow_override_applied:
                        context.output = self._handle_blocked_rail_response(response)
                    else:
                        context.output = os.getenv(
                            "GUARDRAILS_INPUT_BLOCK_MESSAGE",
                            "I'm sorry, I can't help with that request.",
                        )
                    return context

                if modified:
                    args = list(context.modified_args)
                    args[0] = _replace_latest_user_text(value, result_text)
                    context.modified_args = tuple(args)
                    return context

                return None

            except Exception as error:
                span.record_exception(error)
                span.set_status(Status(StatusCode.ERROR, str(error)))
                raise

    async def _stream_with_output_rails(
        self,
        ctx: InvocationContext,
        call_next: CallNextStream,
    ) -> AsyncIterator[Any]:
        """Sanitize clean text chunks and record output-rail decisions."""

        await self.bind_llms_to_rail()

        input_text = _input_text(ctx.modified_args[0]) if ctx.modified_args else ""
        messages: list[dict[str, str]] = (
            [{"role": "user", "content": input_text}] if input_text else []
        )

        stream_state: dict[str, Any] = {
            "is_chat_chunk": False,
            "id": None,
            "created": None,
            "model": None,
            "system_fingerprint": None,
            "service_tier": None,
        }
        raw_parts: list[str] = []
        guarded_parts: list[str] = []
        blocked = False
        block_message = ""

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
                        raw_parts.append(text)
                        yield text
                    continue

                if isinstance(item, str):
                    if item:
                        raw_parts.append(item)
                        yield item
                    continue

                text = str(item)
                if text:
                    raw_parts.append(text)
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

        with tracer.start_as_current_span("guardrail.output.regex_presidio") as span:
            _set_common_guardrail_attributes(
                span,
                stage="output",
                name="regex + Presidio output",
            )
            span.set_attribute("guardrail.type", "deterministic_output_pipeline")
            span.set_attribute(
                "guardrail.configured_rails",
                _json(["regex check output", "mask sensitive data on output"]),
            )

            try:
                async for guarded_text in self._llm_rails.stream_async(
                    messages=messages,
                    generator=upstream_text(),
                ):
                    try:
                        payload = json.loads(guarded_text)
                        if isinstance(payload, dict) and "error" in payload:
                            error = payload.get("error") or {}
                            block_message = error.get(
                                "message",
                                "Blocked by output rail.",
                            )
                            blocked = True
                            ctx.output = ""
                            blocked_output = self.on_post_invoke_blocked(
                                ctx,
                                block_message,
                            )
                            if isinstance(blocked_output, str):
                                guarded_parts.append(blocked_output)
                                yield wrap_text(blocked_output)
                            else:
                                yield blocked_output
                            break
                    except (json.JSONDecodeError, TypeError, ValueError):
                        pass

                    if guarded_text:
                        guarded_parts.append(guarded_text)
                        yield wrap_text(guarded_text)

                raw_text = "".join(raw_parts)
                guarded_text_full = "".join(guarded_parts)
                modified = not blocked and raw_text != guarded_text_full
                overall_outcome = (
                    "blocked" if blocked else "modified" if modified else "passed"
                )
                regex_outcome = "blocked" if blocked else "passed"
                presidio_outcome = (
                    "skipped" if blocked else "modified" if modified else "passed"
                )

                span.set_attribute("guardrail.outcome", overall_outcome)
                span.set_attribute("guardrail.blocked", blocked)
                span.set_attribute("guardrail.modified", modified)
                span.set_attribute("guardrail.regex.outcome", regex_outcome)
                span.set_attribute("guardrail.presidio.outcome", presidio_outcome)
                span.set_attribute("guardrail.input.length", len(raw_text))
                span.set_attribute("guardrail.output.length", len(guarded_text_full))
                span.set_attribute("guardrail.input.sha256", _sha256(raw_text))
                span.set_attribute("guardrail.output.sha256", _sha256(guarded_text_full))
                if block_message:
                    span.set_attribute("guardrail.block_message", _truncate(block_message))

                output_payload: dict[str, Any] = {
                    "outcome": overall_outcome,
                    "blocked": blocked,
                    "modified": modified,
                    "regex": {"outcome": regex_outcome},
                    "presidio": {"outcome": presidio_outcome},
                    "input_length": len(raw_text),
                    "output_length": len(guarded_text_full),
                    "block_message": block_message or None,
                }
                if _env_bool("GUARDRAILS_TRACE_CAPTURE_CONTENT", True):
                    output_payload["sanitized_output"] = guarded_text_full
                if _env_bool("GUARDRAILS_TRACE_CAPTURE_RAW_OUTPUT", False):
                    # Off by default: pre-mask output can contain exactly the PII
                    # or secret that this rail exists to prevent from escaping.
                    output_payload["raw_output_before_guardrails"] = raw_text

                span.set_attribute("input.value", _json({
                    "raw_output_sha256": _sha256(raw_text),
                    "raw_output_length": len(raw_text),
                    "raw_output_captured": _env_bool(
                        "GUARDRAILS_TRACE_CAPTURE_RAW_OUTPUT",
                        False,
                    ),
                }))
                span.set_attribute("input.mime_type", "application/json")
                span.set_attribute("output.value", _json(output_payload))
                span.set_attribute("output.mime_type", "application/json")

                span.add_event(
                    "guardrail.rail_result",
                    {
                        "guardrail.name": "regex check output",
                        "guardrail.outcome": regex_outcome,
                        "guardrail.blocked": blocked,
                    },
                )
                span.add_event(
                    "guardrail.rail_result",
                    {
                        "guardrail.name": "mask sensitive data on output",
                        "guardrail.outcome": presidio_outcome,
                        "guardrail.modified": modified,
                    },
                )
                span.set_status(Status(StatusCode.OK))

            except Exception as error:
                span.record_exception(error)
                span.set_status(Status(StatusCode.ERROR, str(error)))
                raise


@register_middleware(config_type=TextGuardrailsMiddlewareConfig)
async def text_guardrails_middleware(
    config: TextGuardrailsMiddlewareConfig,
    builder: Builder,
) -> AsyncGenerator[TextGuardrailsMiddleware, None]:
    """Register the text-aware and observable Guardrails middleware."""

    middleware = TextGuardrailsMiddleware(config=config, builder=builder)
    await middleware.bind_llms_to_rail()
    yield middleware
