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

import asyncio
import hashlib
import json
import logging
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
from pydantic import model_validator

from nat.builder.builder import Builder
from nat.builder.context import Context
from nat.builder.context import ContextState
from nat.cli.register_workflow import register_middleware
from nat.data_models.api_server import ChatResponse
from nat.data_models.api_server import ChatResponseChoice
from nat.data_models.api_server import ChatResponseChunk
from nat.data_models.api_server import ChoiceMessage
from nat.data_models.api_server import UserMessageContentRoleType
from nat.middleware.function_middleware import CallNextStream
from nat.middleware.middleware import InvocationContext
from nat.plugins.security.middleware.guardrails.nemo_guardrails_middleware import GuardrailsMiddleware
from nat.plugins.security.middleware.guardrails.nemo_guardrails_middleware_config import GuardrailsMiddlewareConfig

from nat_streaming_react.guardrails_compat import RailFlowParameterGuard
from nat_streaming_react.guardrails_compat import RailsPool
from nat_streaming_react.guardrails_compat import register_rail_compatibility
from nat_streaming_react.llm_config import prune_empty_params
from nat_streaming_react.observability import trace_content

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("nat_streaming_react.guardrails", "0.1.9")


class TextGuardrailsMiddlewareConfig(
    GuardrailsMiddlewareConfig,
    name="text_guardrails",
):
    """Guardrails configuration with text-aware chat-stream handling."""

    @model_validator(mode="after")
    def _omit_empty_rail_model_parameters(self) -> "TextGuardrailsMiddlewareConfig":
        """Drop guard-model parameters that were configured empty.

        Same rule, and same reason, as ``llm_config.OptionalParamsOpenAIModelConfig``
        applies to the workflow LLM: ``reasoning_effort`` must be *absent* for a
        provider that does not accept it, and NAT's YAML interpolation can only
        produce a string. Leaving ``LLM_GUARD_REASONING_EFFORT`` empty omits the
        parameter here instead of sending ``""`` inside ``extra_body``.
        """

        models = getattr(getattr(self, "guardrails", None), "models", None) or []
        for model in models:
            parameters = getattr(model, "parameters", None)
            if not isinstance(parameters, dict):
                continue
            pruned = prune_empty_params(parameters)
            if pruned != parameters:
                dropped = sorted(set(parameters) - set(pruned))
                logger.info(
                    "Omitting empty guard-model parameter(s) for %r: %s",
                    getattr(model, "model", "<unnamed>"),
                    ", ".join(dropped) if dropped else "nested values",
                )
                parameters.clear()
                parameters.update(pruned)
        return self


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
        "refund_fraud_evasion",
        re.compile(
            r"\b(?:give|provide|write|show|tell|explain|help)\b.{0,80}"
            r"\b(?:step[- ]by[- ]step|instructions?|method|plan|how to)\b.{0,160}"
            r"\b(?:commit|exploit|abuse|fake|falsify|fraudulently\s+(?:file|claim))\b.{0,160}"
            r"\b(?:refund fraud|chargeback fraud|return fraud|payment fraud|"
            r"fraud detection|loss prevention)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
)


def _is_rail_block_envelope(payload: Any) -> bool:
    """Whether a streamed chunk is NeMo's block signal rather than model text.

    NeMo signals a streaming block by emitting exactly ``{"error": {...}}`` in
    place of a chunk. Matching that envelope precisely matters because this
    middleware relays assistant text that quotes tool results: a model chunk
    that merely happens to be JSON containing an ``error`` key must not be
    mistaken for a rail verdict and truncate the response.
    """

    return (
        isinstance(payload, dict)
        and set(payload) == {"error"}
        and isinstance(payload.get("error"), dict)
    )


def _prior_turn_text(raw: Any) -> str:
    """Client-supplied turns attributed to the *assistant*.

    The self-check LLM evaluates the latest user turn, but the model is handed
    the whole conversation, and the whole conversation comes from the client: the
    gateway validates that each history message has role ``user`` or
    ``assistant``, not who wrote it. A caller can therefore submit fabricated
    assistant turns that no rail ever saw, carrying the implied authority of the
    agent's own voice. Running the deterministic patterns across those turns
    closes that channel without paying for a second LLM call.

    Prior *user* turns are deliberately excluded. Each was screened by the full
    rail when it was the latest turn, and a refused one never reached the model —
    but its text stays in the history the client replays. Screening it again made
    one refusal poison the rest of the conversation: every later message, however
    innocuous, matched the injection still sitting in the transcript and was
    refused. Recovery after a blocked turn is a required behaviour, so prior user
    turns are not re-screened.

    The residual gap is a fabricated prior *user* turn, which no rail sees
    either. It is knowingly left open: closing it means re-screening text the
    user can see was already refused, and the same caller can simply send that
    text as the latest turn, where the full rail does screen it.
    """

    messages = getattr(raw, "messages", None)
    if not messages:
        return ""

    collected: list[str] = []
    for message in messages:
        role = getattr(message, "role", None)
        role_value = getattr(role, "value", role)
        if role_value == "user":
            continue
        content = _content_to_text(getattr(message, "content", None))
        if content:
            collected.append(content)
    return "\n".join(collected)


def _critical_input_matches(text: str) -> list[str]:
    """Return deterministic high-confidence input-policy block matches."""

    if not _env_bool("GUARDRAILS_INPUT_DETERMINISTIC_FALLBACK", True):
        return []
    return [name for name, pattern in _CRITICAL_INPUT_PATTERNS if pattern.search(text)]


_TICKET_ID_PATTERN = r"TKT-[A-Z0-9][A-Z0-9_-]*"
_READ_ONLY_TICKET_TEMPLATES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "list_open_tickets",
        re.compile(
            r"^\s*(?:please\s+)?(?:show|list|display|get|retrieve)\s+"
            r"(?:me\s+)?(?:all\s+)?(?:of\s+)?(?:my\s+)?open\s+(?:support\s+)?tickets?"
            r"(?:\s+(?:with|including)\s+(?:their\s+)?"
            r"(?:details?|status(?:es)?|priority|subjects?))?[.!?]?\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "list_open_ticket_history",
        re.compile(
            r"^\s*(?:please\s+)?(?:show|list|display|get|retrieve)\s+"
            r"(?:me\s+)?(?:all\s+)?(?:the\s+)?(?:history|events?)\s+(?:for|from)\s+"
            r"(?:all\s+)?(?:my\s+)?open\s+tickets?"
            r"(?:\s+(?:with|including)\s+(?:their\s+)?details?)?[.!?]?\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "specific_ticket_direct",
        re.compile(
            rf"^\s*(?:please\s+)?(?:show|display|get|retrieve|summarize)\s+"
            rf"(?:me\s+)?(?:ticket\s+)?{_TICKET_ID_PATTERN}"
            r"(?:\s*,?\s*(?:and\s+)?(?:"
            r"quote\s+its\s+(?:complete\s+|full\s+)?description\s+exactly"
            r"(?:\s*,?\s*including\s+every\s+key\s+and\s+value)?|"
            r"including\s+(?:the\s+)?customer\s+and\s+assigned\s+agent|"
            r"with\s+(?:all\s+)?(?:its\s+)?history))?[.!?]?\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "specific_ticket_details",
        re.compile(
            rf"^\s*(?:please\s+)?(?:show|display|get|retrieve|summarize)\s+"
            r"(?:me\s+)?(?:the\s+)?(?:(?:complete|full)\s+)?"
            r"(?:details?|information)(?:\s+and\s+(?:all\s+)?(?:its\s+)?history)?\s+"
            rf"(?:for|of|from)\s+(?:ticket\s+)?{_TICKET_ID_PATTERN}"
            r"(?:\s*,?\s*(?:including|with)\s+(?:the\s+)?(?:"
            r"customer\s+and\s+assigned\s+agent|customer|assigned\s+agent|"
            r"(?:its\s+)?(?:all\s+)?history))?[.!?]?\s*$",
            re.IGNORECASE,
        ),
    ),
    (
        "specific_ticket_history",
        re.compile(
            rf"^\s*(?:please\s+)?(?:show|list|display|get|retrieve)\s+"
            r"(?:me\s+)?(?:all\s+)?(?:the\s+)?(?:history|events?)\s+(?:for|of|from)\s+"
            rf"(?:ticket\s+)?{_TICKET_ID_PATTERN}[.!?]?\s*$",
            re.IGNORECASE,
        ),
    ),
)


def _read_only_ticket_allow_matches(text: str) -> list[str]:
    """Recognize tightly scoped, read-only ticket lookup requests.

    Every pattern is anchored to the complete message. This prevents an attacker
    from appending an instruction override or harmful request to an otherwise
    valid ticket query and then benefiting from the allow override.
    """

    if not _env_bool("GUARDRAILS_INPUT_READ_ONLY_ALLOW_OVERRIDE", True):
        return []

    normalized = " ".join(text.split())
    if not normalized or len(normalized) > 500:
        return []

    return [
        name
        for name, pattern in _READ_ONLY_TICKET_TEMPLATES
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


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean switch, refusing to guess.

    Anything unrecognised used to become ``False``, which for
    ``GUARDRAILS_INPUT_DETERMINISTIC_FALLBACK`` meant a typo silently switched
    off the deterministic block patterns. An unreadable value now keeps the
    declared default — the safe setting for every flag here — and says so.
    """

    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    logger.warning(
        "%s=%r is not a recognised boolean; keeping the default (%s)", name, raw, default
    )
    return default


def _guard_model_name() -> str:
    """The model the input rail classifies with, for the decision span.

    Reads ``LLM_GUARD_MODEL`` first, falling back to ``LLM_MODEL`` since the
    guard model defaults to the agent model when not set separately.
    """

    for name in ("LLM_GUARD_MODEL", "LLM_MODEL"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return "unknown"


def _trace_limit() -> int:
    try:
        return max(int(os.getenv("GUARDRAILS_TRACE_MAX_CHARS", "16384")), 256)
    except ValueError:
        return 16384


#: Default ceiling on how much streamed text is buffered before PII masking
#: runs. Sized well above any real answer in this sample application, so it
#: practically never trips on legitimate output; it exists to bound memory
#: and give an oversized response a defined, safe outcome instead of an
#: unbounded buffer.
_DEFAULT_PII_MAX_BUFFER_CHARS = 200_000


def _pii_buffer_limit() -> int:
    try:
        return max(int(os.getenv("GUARDRAILS_PII_MAX_BUFFER_CHARS", str(_DEFAULT_PII_MAX_BUFFER_CHARS))), 1024)
    except ValueError:
        logger.warning(
            "GUARDRAILS_PII_MAX_BUFFER_CHARS is not an integer; using %d",
            _DEFAULT_PII_MAX_BUFFER_CHARS,
        )
        return _DEFAULT_PII_MAX_BUFFER_CHARS


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


def _emit_nat_evaluation_event(name: str, input_data: Any, output_data: Any) -> None:
    """Expose a compact machine-readable decision through NAT intermediate events.

    MLflow evaluation calls the live ``/v1/workflow/full`` stream. NAT already
    exposes tool calls there as FUNCTION/TOOL intermediate events. Emitting a
    small function event for each guardrail decision lets the evaluator score
    the exact decision from the same response without querying MLflow traces
    asynchronously or inferring a block from refusal wording. The assistant-ui
    bridge ignores these function names, so they remain evaluation metadata.
    """

    try:
        nat_context = Context(ContextState.get())
        with nat_context.push_active_function(name, input_data) as step:
            step.set_output(output_data)
    except Exception:
        # Observability metadata must never change the safety decision or break
        # a user request. The OpenTelemetry guardrail span remains authoritative.
        return


class TextGuardrailsMiddleware(GuardrailsMiddleware):
    """Apply NeMo rails to text while preserving NAT chat chunk types."""

    def __init__(self, config: TextGuardrailsMiddlewareConfig, builder: Builder) -> None:
        super().__init__(config, builder)
        # The pinned NeMo Guardrails release declares the regex output action
        # without a blocking output mapping, and both the regex and the Presidio
        # masking actions without **kwargs, which makes the streaming rails a
        # silent no-op or a TypeError. Re-register corrected declarations through
        # the supported extension point instead of editing site-packages.
        self.rail_compatibility_applied = register_rail_compatibility(self._llm_rails)
        # The same release resolves $bot_message into the shared flow config, so
        # without this guard every request after the first would be evaluated
        # against the first request's text.
        self.rail_flow_guard = RailFlowParameterGuard(self._llm_rails)
        # ...and the guard alone only makes *sequential* reuse safe. Concurrent
        # evaluations must not share an instance at all, so every rail run leases
        # one from this pool. See RailsPool.
        self.rails_pool = RailsPool(self._build_rails)

    def _build_rails(self) -> Any:
        """Construct an isolated rails instance equivalent to the primary one."""

        from nemoguardrails import LLMRails

        rails = LLMRails(self._guardrails_config.guardrails.model_copy(deep=True))
        register_rail_compatibility(rails)

        # Carry over whatever NAT bound onto the primary instance — rail LLM
        # bindings live here. Absent in the default deployment, where the rail
        # model comes from the guardrails config itself, but a pooled instance
        # must never silently lose a binding the primary has.
        params = getattr(
            getattr(self._llm_rails, "runtime", None), "registered_action_params", None
        )
        if isinstance(params, dict):
            for name, value in params.items():
                rails.register_action_param(name, value)
        elif getattr(self._config, "llm_bindings", None):
            raise RuntimeError(
                "Guardrails rail LLM bindings are configured but cannot be copied onto "
                "a pooled rails instance; concurrent rail isolation would lose them."
            )
        return rails

    def _pii_masking_enabled(self) -> bool:
        """Whether the PII-masking output flow is configured.

        Independent of whether the secret-blocking regex flow is configured:
        each is its own entry in ``output.flows``, so a deployment can run
        either, both, or neither without affecting the other's availability.
        This flag alone decides streaming mode vs. buffered mode below,
        because masking — unlike the regex check — cannot run token-by-token
        (see ``_stream_with_buffered_masking``).
        """

        output = getattr(getattr(self._guardrails_config.guardrails, "rails", None), "output", None)
        flows = getattr(output, "flows", None) or []
        return "mask sensitive data on output" in flows

    async def pre_invoke(self, context: InvocationContext) -> InvocationContext | None:
        """Run input rails on the latest user message and record the verdict."""

        await self.bind_llms_to_rail()

        run_id = trace_content.current_run_id()

        if not context.modified_args or context.modified_args[0] is None:
            return None

        value: Any = context.modified_args[0]
        text = _input_text(value)
        # Recorded here, not only in the workflow function: when the input
        # rail blocks below, `call_next` (the workflow function) never runs,
        # so this is the only place that ever sees the question for a blocked
        # request.
        trace_content.record(run_id, question=text)

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
            _emit_nat_evaluation_event(
                "guardrail_input_self_check_decision",
                {"user_message_present": False},
                {
                    "stage": "input",
                    "outcome": "skipped",
                    "blocked": False,
                    "skip_reason": "no_user_text_extracted",
                },
            )
            return None

        async with self.rails_pool.acquire() as rails:
            result = await self._check_input(context, value, text, rails)

        # `result.output` is a string only on the blocked branch (the modified
        # branch changes `modified_args`, not `output`); that string is the
        # refusal the caller actually receives, and neither `_response_fn` nor
        # `_stream_fn` will ever run to record an answer of their own.
        if result is not None and isinstance(result.output, str):
            trace_content.record(run_id, answer=result.output)

        return result

    async def _check_input(
        self,
        context: InvocationContext,
        value: Any,
        text: str,
        rails: Any,
    ) -> InvocationContext | None:
        """Evaluate the input rails on an instance no other request is using."""

        rendered_prompt = _rendered_self_check_prompt(rails, text)
        # History matches are labelled so a span shows which turn decided, and
        # they join the deterministic block set so they win over the read-only
        # allow override exactly as a match on the latest turn would.
        deterministic_matches = _critical_input_matches(text) + [
            f"history:{name}" for name in _critical_input_matches(_prior_turn_text(value))
        ]
        deterministic_allow_matches = _read_only_ticket_allow_matches(text)

        with tracer.start_as_current_span("guardrail.input.self_check") as span:
            _set_common_guardrail_attributes(
                span,
                stage="input",
                name="self check input",
            )
            span.set_attribute("guardrail.type", "llm_self_check_with_deny_and_read_only_allow_overrides")
            span.set_attribute("gen_ai.operation.name", "guardrail_check")
            # The endpoint is any OpenAI-compatible one, so the provider is not
            # knowable from here; the base URL is what identifies it.
            span.set_attribute("gen_ai.provider.name", "openai_compatible")
            span.set_attribute("gen_ai.request.model", _guard_model_name())
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
                response: GenerationResponse = await rails.generate_async(
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
                # 2. A narrow read-only ticket allow rule can correct an LLM
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

                evaluation_decision = {
                    "stage": "input",
                    "name": "self check input",
                    "outcome": outcome,
                    "blocked": blocked,
                    "modified": modified,
                    "llm_blocked": llm_blocked,
                    "deterministic_blocked": bool(deterministic_matches),
                    "deterministic_block_matches": deterministic_matches,
                    "deterministic_allow_matches": deterministic_allow_matches,
                    "allow_override_applied": allow_override_applied,
                    "decision_source": decision_source,
                    "llm_call_count": len(llm_calls),
                }
                _emit_nat_evaluation_event(
                    "guardrail_input_self_check_decision",
                    {
                        "user_message_sha256": _sha256(text),
                        "user_message_length": len(text),
                    },
                    evaluation_decision,
                )

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

    async def post_invoke(self, context: InvocationContext) -> InvocationContext | None:
        """Run output rails on a non-streaming response, then record what is released.

        ``GuardrailsMiddleware.post_invoke`` (inherited, not overridden below)
        may block or mask ``context.output`` in place before returning. It runs
        strictly after the workflow function produced its raw text, so whatever
        survives that call — not the raw text — is what the caller actually
        receives. This also covers the "output rails disabled" case: when
        ``stream_output_rails`` is false, NAT buffers a streamed response and
        still routes it through this same method, so the recorded answer is
        correct there too, whatever it turns out to contain.

        ``context.output`` is set to the *raw*, pre-protection text by the
        caller (``function_middleware_invoke``) before this method ever runs,
        and stays that way for as long as protection is still running.
        Recording it unconditionally in a ``finally`` — the previous shape of
        this method — would put that raw text on the readable trace whenever
        the rail evaluation itself raised, before it ever had a chance to
        block or mask anything. Every branch below records only after
        protection has *succeeded*: on exception, this method records a
        content-free failure status and re-raises without recording anything
        else, which also means nothing unprotected is released downstream
        either — the exception propagates out of
        ``function_middleware_invoke`` before it reaches ``return
        ctx.output``.

        ``context.output`` can also be a structured ``ChatResponse`` (the
        workflow function returns one whenever the inbound request was a
        ``ChatRequest`` rather than a bare string). NAT's own field selector
        only looks at a Pydantic model's *top-level* fields, so it would
        forward plain metadata strings (``id``, ``model``,
        ``system_fingerprint``) to the rail as if they were the answer, and —
        critically — never reach ``choices[].message.content`` at all, since
        that is a list of ``ChatResponseChoice`` objects, not of strings, and
        the selector's list-handling only picks up string *items*. The actual
        assistant answer would pass through completely unprotected. That case
        is handled separately, by ``_post_invoke_chat_response``.
        """

        run_id = trace_content.current_run_id()

        if isinstance(context.output, ChatResponse):
            return await self._post_invoke_chat_response(context, run_id)

        try:
            result = await super().post_invoke(context)
        except asyncio.CancelledError:
            trace_content.record(
                run_id,
                error="CancelledError: request cancelled during output guardrail evaluation",
            )
            raise
        except Exception as error:
            # Type name only, never the exception's own message: some
            # exceptions (serialization errors, validation errors) echo the
            # value that failed, which here could be the very answer this
            # method exists to protect.
            trace_content.record(
                run_id,
                error=f"{type(error).__name__}: output guardrail evaluation failed",
            )
            raise

        released_text = context.output if isinstance(context.output, str) else None
        if released_text is not None:
            trace_content.record(run_id, answer=released_text)
        return result

    async def _post_invoke_chat_response(
        self,
        context: InvocationContext,
        run_id: str | None,
    ) -> InvocationContext | None:
        """Protect assistant text nested inside a structured ``ChatResponse``.

        Runs the identical rail evaluation the plain-string path uses
        (``generate_async`` with ``rails=["output"]``), once per choice's
        assistant text, and rebuilds the response with only ``choices``
        replaced — ``id``, ``model``, ``usage``, ``system_fingerprint`` and
        every other field are carried over untouched, so structure, metadata
        and API compatibility are preserved. Each choice is protected
        *independently*: ``choices`` represents parallel alternative
        completions (OpenAI's ``n`` semantics), not sequential parts of one
        answer, so a secret or blocked entity in one alternative replaces
        only that alternative's content, with its ``finish_reason`` set to
        ``"content_filter"`` — it does not discard the other, unrelated
        alternatives. A choice with no content (a tool-only turn, for
        instance) has nothing to protect and is passed through unchanged; a
        content shape this method cannot recognise as text is refused the
        same way a blocked choice is, since it cannot be inspected and must
        never be released unprotected.

        ``context.output`` is reassigned only after every choice has been
        evaluated without a guardrail exception. On exception — including
        cancellation — nothing is reassigned and nothing is recorded beyond a
        content-free failure status, so a partially protected or entirely
        unprotected response can never reach the caller or the readable
        trace; the exception simply propagates.
        """

        response: ChatResponse = context.output
        input_text = _input_text(context.modified_args[0]) if context.modified_args else ""
        messages_prefix: list[dict[str, str]] = (
            [{"role": "user", "content": input_text}] if input_text else []
        )

        protected_choices: list[ChatResponseChoice] = []
        answer_parts: list[str] = []

        def _blocked_choice(choice: ChatResponseChoice, message: str) -> ChatResponseChoice:
            refusal = self.on_post_invoke_blocked(context, message)
            refusal_text = refusal if isinstance(refusal, str) else ("" if refusal is None else str(refusal))
            answer_parts.append(refusal_text)
            return choice.model_copy(
                update={
                    "message": choice.message.model_copy(update={"content": refusal_text}),
                    "finish_reason": "content_filter",
                }
            )

        try:
            for choice in response.choices:
                text = choice.message.content
                if text is None:
                    protected_choices.append(choice)
                    continue
                if not isinstance(text, str):
                    # Not a shape this method can inspect. Refuse rather than
                    # release it unprotected -- an unsupported form must
                    # never silently bypass protection.
                    protected_choices.append(
                        _blocked_choice(
                            choice,
                            "The response contained a form of content that could "
                            "not be safety-checked and was withheld.",
                        )
                    )
                    continue

                async with self.rails_pool.acquire() as rails:
                    rail_response: GenerationResponse = await rails.generate_async(
                        messages=messages_prefix + [{"role": "assistant", "content": text}],
                        options=GenerationOptions(
                            rails=["output"],
                            log=GenerationLogOptions(activated_rails=True),
                            output_vars=["bot_message", "user_message"],
                        ),
                    )

                if self._rail_blocked(rail_response):
                    protected_choices.append(
                        _blocked_choice(choice, self._handle_blocked_rail_response(rail_response))
                    )
                    continue

                protected_text = self._handle_modified_rail_response(rail_response, fallback=text)
                answer_parts.append(protected_text)
                protected_choices.append(
                    choice.model_copy(
                        update={"message": choice.message.model_copy(update={"content": protected_text})}
                    )
                )
        except asyncio.CancelledError:
            trace_content.record(
                run_id,
                error="CancelledError: request cancelled during output guardrail evaluation",
            )
            raise
        except Exception as error:
            trace_content.record(
                run_id,
                error=f"{type(error).__name__}: output guardrail evaluation failed",
            )
            raise

        context.output = response.model_copy(update={"choices": protected_choices})
        trace_content.record(run_id, answer="\n".join(answer_parts))
        return context

    async def _stream_with_output_rails(
        self,
        ctx: InvocationContext,
        call_next: CallNextStream,
    ) -> AsyncIterator[Any]:
        """Dispatch to the streaming or buffered output-rail path.

        PII masking cannot run token-by-token: NeMo's streaming rail runner
        only ever uses an action's return value to decide block/no-block, so
        a masking action's rewritten text is discarded and the unmodified
        upstream chunk is what actually gets released (see
        ``_stream_with_output_rails_incremental``, and the module docstring).
        Buffering the complete answer and masking it once, in
        ``_stream_with_buffered_masking``, is the only way this configuration
        can keep the promise its own name makes. When masking is not
        configured, the regex secret check alone needs none of that — it
        already blocks correctly on a rolling window of chunks — so it keeps
        running with full per-token streaming latency.
        """

        if self._pii_masking_enabled():
            async for item in self._stream_with_buffered_masking(ctx, call_next):
                yield item
            return

        async for item in self._stream_with_output_rails_incremental(ctx, call_next):
            yield item

    async def _stream_with_output_rails_incremental(
        self,
        ctx: InvocationContext,
        call_next: CallNextStream,
    ) -> AsyncIterator[Any]:
        """Sanitize clean text chunks and record output-rail decisions.

        This is the true output boundary for a streaming response: everything
        this method yields is text the caller actually receives, and nothing
        it does not yield ever reaches them. ``released`` accumulates exactly
        that — not the raw pre-rail text `call_next` produced — so the
        readable trace answer reflects masking and blocking correctly. The
        ``finally`` below records it whether the stream finishes normally, the
        output rail blocks partway through, an exception propagates from
        upstream or from the rail itself, or the caller cancels the generator
        (a client disconnect), so a partial answer is never silently dropped.

        Used only when PII masking is not configured (see
        ``_pii_masking_enabled``): the regex secret check alone is safe to run
        this way, on a rolling window of chunks, because blocking does not
        need to rewrite text — it only needs to decide whether to release it.
        """

        await self.bind_llms_to_rail()

        run_id = trace_content.current_run_id()
        released = trace_content.StreamTextAccumulator()

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

        try:
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
                    # One instance per concurrent stream. Sharing one made a chunk
                    # get checked against another response's text, and a credential
                    # walked through untouched while the rail reported nothing.
                    async with self.rails_pool.acquire() as rails:
                        async for guarded_text in rails.stream_async(
                            messages=messages,
                            generator=upstream_text(),
                        ):
                            try:
                                payload = json.loads(guarded_text)
                                if _is_rail_block_envelope(payload):
                                    error = payload["error"]
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
                                        released.add(blocked_output)
                                        yield wrap_text(blocked_output)
                                    else:
                                        blocked_text = (
                                            _content_from_chat_chunk(blocked_output)
                                            if isinstance(blocked_output, ChatResponseChunk)
                                            else str(blocked_output)
                                            if blocked_output is not None
                                            else ""
                                        )
                                        if blocked_text:
                                            released.add(blocked_text)
                                        yield blocked_output
                                    break
                            except (json.JSONDecodeError, TypeError, ValueError):
                                pass

                            if guarded_text:
                                guarded_parts.append(guarded_text)
                                released.add(guarded_text)
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

                    _emit_nat_evaluation_event(
                        "guardrail_output_regex_presidio_decision",
                        {
                            "raw_output_sha256": _sha256(raw_text),
                            "raw_output_length": len(raw_text),
                        },
                        {
                            "stage": "output",
                            "name": "regex + Presidio output",
                            "outcome": overall_outcome,
                            "blocked": blocked,
                            "modified": modified,
                            "regex_outcome": regex_outcome,
                            "presidio_outcome": presidio_outcome,
                            "sanitized_output_length": len(guarded_text_full),
                        },
                    )

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
        finally:
            # Runs on normal completion, on a mid-stream block, on any
            # exception (upstream or from the rail itself), and on
            # cancellation from a client disconnect — the accumulator already
            # holds exactly the text yielded above, whatever the reason this
            # generator stopped.
            trace_content.record(
                run_id,
                answer=released.text,
                answer_truncated=released.truncated,
            )

    async def _stream_with_buffered_masking(
        self,
        ctx: InvocationContext,
        call_next: CallNextStream,
    ) -> AsyncIterator[Any]:
        """Buffer the whole answer, mask it once, then release it.

        Streaming masking is not "not yet implemented" — it is not something
        NeMo's streaming rail runner can do at all (see
        ``_stream_with_output_rails``'s docstring and the module docstring).
        Releasing chunks first and masking afterwards is not an option either:
        by the time a chunk has left this method it has already reached the
        client, and an entity can straddle any two adjacent chunks regardless
        of where the model happened to break its tokens. The only boundary
        that is always safe to detect PII against is the complete answer, so
        that is what this buffers before evaluating anything.

        Nothing is yielded until the blocking-capable, non-streaming rail
        evaluation (the exact one the non-streaming path uses) has completely
        finished — a masked/blocked/error outcome never has an unmasked
        prefix already sitting with the client. A buffer that grows past
        ``GUARDRAILS_PII_MAX_BUFFER_CHARS`` is treated as a protection failure
        and refused, the same as a rail-blocked response, rather than
        released partially masked or not masked at all.
        """

        await self.bind_llms_to_rail()

        run_id = trace_content.current_run_id()
        released = trace_content.StreamTextAccumulator()

        input_text = _input_text(ctx.modified_args[0]) if ctx.modified_args else ""

        stream_state: dict[str, Any] = {
            "is_chat_chunk": False,
            "id": None,
            "created": None,
            "model": None,
            "system_fingerprint": None,
            "service_tier": None,
        }

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

        limit = _pii_buffer_limit()
        raw_parts: list[str] = []
        raw_length = 0
        oversized = False

        upstream = call_next(*ctx.modified_args, **ctx.modified_kwargs)
        try:
            async for item in upstream:
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
                elif isinstance(item, str):
                    text = item
                else:
                    text = str(item)
                if text:
                    raw_parts.append(text)
                    raw_length += len(text)
                if raw_length > limit:
                    oversized = True
                    break
        finally:
            if oversized:
                # The generator was not exhausted; close it explicitly rather
                # than leaving cleanup to garbage collection.
                await upstream.aclose()

        raw_text = "".join(raw_parts)
        final_text = ""
        block_message = ""
        blocked = False

        try:
            with tracer.start_as_current_span("guardrail.output.regex_presidio") as span:
                _set_common_guardrail_attributes(
                    span,
                    stage="output",
                    name="regex + Presidio output (buffered)",
                )
                span.set_attribute("guardrail.type", "buffered_output_pipeline")
                span.set_attribute(
                    "guardrail.configured_rails",
                    _json(["regex check output", "mask sensitive data on output"]),
                )
                span.set_attribute("guardrail.buffered", True)
                span.set_attribute("guardrail.buffer.limit_chars", limit)
                span.set_attribute("guardrail.buffer.oversized", oversized)

                try:
                    if oversized:
                        blocked = True
                        block_message = (
                            "The response was too large to safely apply PII "
                            "protection and was withheld."
                        )
                        final_text = self.on_post_invoke_blocked(ctx, block_message)
                        if not isinstance(final_text, str):
                            final_text = "" if final_text is None else str(final_text)
                    else:
                        messages: list[dict[str, str]] = (
                            [{"role": "user", "content": input_text}] if input_text else []
                        )
                        messages.append({"role": "assistant", "content": raw_text})
                        async with self.rails_pool.acquire() as rails:
                            response: GenerationResponse = await rails.generate_async(
                                messages=messages,
                                options=GenerationOptions(
                                    rails=["output"],
                                    log=GenerationLogOptions(activated_rails=True),
                                    output_vars=["bot_message", "user_message"],
                                ),
                            )
                        blocked = self._rail_blocked(response)
                        if blocked:
                            block_message = self._handle_blocked_rail_response(response)
                            final_text = self.on_post_invoke_blocked(ctx, block_message)
                            if not isinstance(final_text, str):
                                final_text = "" if final_text is None else str(final_text)
                        else:
                            final_text = self._handle_modified_rail_response(
                                response, fallback=raw_text
                            )

                    modified = not blocked and final_text != raw_text
                    overall_outcome = (
                        "blocked" if blocked else "modified" if modified else "passed"
                    )

                    span.set_attribute("guardrail.outcome", overall_outcome)
                    span.set_attribute("guardrail.blocked", blocked)
                    span.set_attribute("guardrail.modified", modified)
                    span.set_attribute("guardrail.input.length", len(raw_text))
                    span.set_attribute("guardrail.output.length", len(final_text))
                    span.set_attribute("guardrail.input.sha256", _sha256(raw_text))
                    span.set_attribute("guardrail.output.sha256", _sha256(final_text))
                    if block_message:
                        span.set_attribute("guardrail.block_message", _truncate(block_message))

                    output_payload: dict[str, Any] = {
                        "outcome": overall_outcome,
                        "blocked": blocked,
                        "modified": modified,
                        "buffered": True,
                        "oversized": oversized,
                        "input_length": len(raw_text),
                        "output_length": len(final_text),
                        "block_message": block_message or None,
                    }
                    if _env_bool("GUARDRAILS_TRACE_CAPTURE_CONTENT", True):
                        output_payload["sanitized_output"] = final_text
                    if _env_bool("GUARDRAILS_TRACE_CAPTURE_RAW_OUTPUT", False):
                        output_payload["raw_output_before_guardrails"] = raw_text
                    span.set_attribute("output.value", _json(output_payload))
                    span.set_attribute("output.mime_type", "application/json")

                    _emit_nat_evaluation_event(
                        "guardrail_output_regex_presidio_decision",
                        {
                            "raw_output_sha256": _sha256(raw_text),
                            "raw_output_length": len(raw_text),
                        },
                        {
                            "stage": "output",
                            "name": "regex + Presidio output (buffered)",
                            "outcome": overall_outcome,
                            "blocked": blocked,
                            "modified": modified,
                            "buffered": True,
                            "oversized": oversized,
                        },
                    )
                    span.set_status(Status(StatusCode.OK))

                except Exception as error:
                    span.record_exception(error)
                    span.set_status(Status(StatusCode.ERROR, str(error)))
                    raise

            if final_text:
                released.add(final_text)
                yield wrap_text(final_text)
        finally:
            # Runs whether the buffered evaluation finished normally, blocked,
            # was refused as oversized, raised, or the caller cancelled before
            # any of this yielded — the accumulator holds exactly the text (if
            # any) that reached that yield, never the raw buffer.
            trace_content.record(
                run_id,
                answer=released.text,
                answer_truncated=released.truncated,
            )


@register_middleware(config_type=TextGuardrailsMiddlewareConfig)
async def text_guardrails_middleware(
    config: TextGuardrailsMiddlewareConfig,
    builder: Builder,
) -> AsyncGenerator[TextGuardrailsMiddleware, None]:
    """Register the text-aware and observable Guardrails middleware."""

    middleware = TextGuardrailsMiddleware(config=config, builder=builder)
    await middleware.bind_llms_to_rail()
    yield middleware
