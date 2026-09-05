# SPDX-License-Identifier: Apache-2.0
"""NAT telemetry processors: normalize the root span, redact secrets.

``nat.observability.processor.Processor`` is NAT's supported hook for
transforming spans before export. A processor added at position 0 of an
``OtelSpanExporter`` pipeline sees the finished ``Span`` while it is still
NAT's own model, ahead of the OTLP conversion — the right place to change what
telemetry says without changing what the runtime does.

Two processors live here:

``WorkflowContentProcessor``
    Replaces the workflow root span's raw boundary values with the readable
    question and answer captured by the workflow function, and bounds both.
    Only the workflow root is touched: tool, LLM and function spans keep NAT's
    native payloads, because those are what makes a tool call debuggable.

``SensitiveHeaderRedactionProcessor``
    NAT's span exporter copies the full inbound request — headers included —
    into span metadata. The front-end worker already strips ``Authorization``
    from the ASGI scope before NAT can see it, so this is the second layer: an
    explicit deny-list applied to every span, on the principle that a
    credential must have to get past two independent controls to be exported.
"""

import json
import logging
from typing import Any

from nat.data_models.span import Span
from nat.data_models.span import SpanAttributes
from nat.observability.processor.processor import Processor

from nat_streaming_react.observability import trace_content

logger = logging.getLogger(__name__)

WORKFLOW_START_EVENT = "WORKFLOW_START"

REDACTED = "[redacted]"

#: Header names never recorded in telemetry, whatever their value.
#:
#: Credentials, plus the one identity header that carries personal data. The
#: user's subject id and roles stay visible because they are what makes a
#: trace attributable; an email address adds nothing a trace needs and follows
#: the span into whatever backend stores it.
SENSITIVE_HEADERS: frozenset[str] = frozenset({
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "api-key",
    "x-auth-token",
    "x-csrf-token",
    "x-authenticated-email",
})


class WorkflowContentProcessor(Processor[Span, Span]):
    """Make the workflow root span carry the readable question and answer."""

    def __init__(self, span_prefix: str = "nat") -> None:
        self._event_type_key = f"{span_prefix}.event_type"
        self._run_id_key = f"{span_prefix}.workflow.run_id"
        self._truncated_key = f"{span_prefix}.trace.content_truncated"

    async def process(self, item: Span) -> Span:
        if item.attributes.get(self._event_type_key) != WORKFLOW_START_EVENT:
            return item

        recorded = trace_content.pop(item.attributes.get(self._run_id_key))
        if not recorded:
            return item

        question = recorded.get("question")
        if isinstance(question, str) and question:
            bounded, truncated = trace_content.bound(question)
            item.set_attribute(SpanAttributes.INPUT_VALUE.value, bounded)
            item.set_attribute(SpanAttributes.INPUT_MIME_TYPE.value, "text/plain")
            # NAT stores the raw request object alongside the display value.
            # Drop it: it is the unreadable form this processor exists to replace.
            item.attributes.pop("input.value_obj", None)
            if truncated:
                item.set_attribute(self._truncated_key, True)

        answer = recorded.get("answer")
        if isinstance(answer, str) and answer:
            bounded, truncated = trace_content.bound(answer)
            item.set_attribute(SpanAttributes.OUTPUT_VALUE.value, bounded)
            item.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE.value, "text/plain")
            # Same reasoning as the input: NAT's raw form here is the truncated
            # 50-chunk preview that the reconstructed answer replaces.
            item.attributes.pop("output.value_obj", None)
            if truncated or recorded.get("answer_truncated"):
                item.set_attribute(self._truncated_key, True)

        error = recorded.get("error")
        if isinstance(error, str) and error:
            # A failed run still gets a readable root span: what was asked, and
            # what went wrong, rather than an empty output.
            item.set_attribute(SpanAttributes.OUTPUT_VALUE.value, trace_content.bound(error)[0])
            item.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE.value, "text/plain")

        return item


class SensitiveHeaderRedactionProcessor(Processor[Span, Span]):
    """Remove credential-bearing headers from span metadata before export.

    NAT serializes span metadata to a JSON string attribute (``nat.metadata``),
    so redaction parses it, walks the structure and re-serializes. A value that
    cannot be parsed is left untouched rather than dropped: telemetry must never
    fail a request, and the front-end worker has already removed the one
    credential this deployment actually sends.
    """

    def __init__(
        self,
        span_prefix: str = "nat",
        sensitive_headers: frozenset[str] = SENSITIVE_HEADERS,
    ) -> None:
        self._sensitive = sensitive_headers
        self._metadata_key = f"{span_prefix}.metadata"

    async def process(self, item: Span) -> Span:
        for key, value in list(item.attributes.items()):
            if isinstance(value, dict):
                item.attributes[key] = self._redact(value)
            elif key == self._metadata_key and isinstance(value, str):
                item.attributes[key] = self._redact_json(value)
        return item

    def _redact_json(self, value: str) -> str:
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError, ValueError):
            return value
        redacted = self._redact(parsed)
        if redacted == parsed:
            return value
        try:
            return json.dumps(redacted, default=str)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return value

    def _redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: (REDACTED if str(key).lower() in self._sensitive else self._redact(item))
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value
