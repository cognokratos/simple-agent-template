# SPDX-License-Identifier: Apache-2.0
"""The question and the answer, captured where they are actually readable.

NAT records the workflow's raw boundary values on the root span: the whole
``ChatRequest`` object for the input, and for a streaming run a preview list of
the first 50 ``ChatResponseChunk`` objects for the output. Both are accurate and
neither is readable, and with per-token streaming the 50-chunk cap truncates a
normal answer after a few words.

The workflow function (``register.py``) is the one place that sees the whole
request object, so that is where the readable *question* is captured. It is
**not** where the readable *answer* is captured: ``TextGuardrailsMiddleware``
(``text_guardrails.py``) wraps that function and runs strictly after it, so
recording the workflow function's own output as "the answer" would capture
pre-rail text — a masked or blocked response could then leak into the trace.
The answer is instead recorded by the middleware itself, at the point where it
actually releases text downstream, using the same accumulator this module
provides. Either way, the stream is never buffered on the client's behalf:
each chunk is yielded first and appended to the accumulator afterwards, so
observability adds no latency and cannot delay or reorder a token.

The captured values are handed to the telemetry pipeline through this registry,
keyed by NAT's workflow run id — a value both sides already have
(``ContextState.workflow_run_id``, exported as the ``nat.workflow.run_id`` span
attribute). The registry is bounded and self-trimming, so a span that never
arrives cannot leak memory.

What is captured, and when
--------------------------
See ``nat_streaming_react.text_guardrails.TextGuardrailsMiddleware`` for the
exact capture points: ``_stream_with_output_rails`` for streaming responses,
``post_invoke`` for non-streaming ones (also used when ``stream_output_rails``
is disabled), and ``pre_invoke`` for a request blocked before the workflow
function ever runs. In every case the recorded answer is the text that
middleware actually released downstream, not raw model output — this is not a
guarantee that the text reached the browser, only that it crossed this
boundary. The guardrail middleware records its own pre/post hashes separately
on its OpenTelemetry span and keeps raw pre-mask text off spans unless
``GUARDRAILS_TRACE_CAPTURE_RAW_OUTPUT`` is explicitly enabled; that switch is
independent of this module and does not change what gets recorded here. This
module says nothing about LLM- or tool-call spans, which are captured and
redacted under their own, separate controls.

Capture is on by default and bounded by ``NAT_TRACE_CONTENT_MAX_CHARS``. Set
``NAT_TRACE_CAPTURE_CONTENT=false`` to record neither question nor answer; span
structure, timings and guardrail decisions are unaffected. Note that disabling
it here does not disable NAT's own raw boundary attributes — it only stops this
package from adding readable ones.
"""

import logging
import os
from collections import OrderedDict
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MAX_CHARS = 65536
TRUNCATION_NOTICE = "\n[trace content truncated]"

#: Bound on retained entries. Each entry is released when its span is exported;
#: this only caps the damage if a workflow dies before its root span is built.
_MAX_PENDING_ENTRIES = 256

_entries: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_lock = Lock()

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def capture_enabled() -> bool:
    """Whether readable question/answer content is recorded at all."""

    raw = os.getenv("NAT_TRACE_CAPTURE_CONTENT")
    if raw is None or not raw.strip():
        return True
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    logger.warning(
        "NAT_TRACE_CAPTURE_CONTENT=%r is not a recognised boolean; keeping the default (True)",
        raw,
    )
    return True


def max_chars() -> int:
    """Maximum characters captured per trace field."""

    raw = os.getenv("NAT_TRACE_CONTENT_MAX_CHARS", str(DEFAULT_MAX_CHARS))
    try:
        return max(int(raw), 256)
    except ValueError:
        logger.warning(
            "NAT_TRACE_CONTENT_MAX_CHARS=%r is not an integer; using %d",
            raw,
            DEFAULT_MAX_CHARS,
        )
        return DEFAULT_MAX_CHARS


def bound(text: str) -> tuple[str, bool]:
    """Return ``(text, truncated)`` bounded to the configured limit."""

    limit = max_chars()
    if len(text) <= limit:
        return text, False
    return text[:limit] + TRUNCATION_NOTICE, True


def record(run_id: str | None, **fields: Any) -> None:
    """Record trace content for one workflow run.

    ``error`` is recorded even when content capture is disabled: it is a failure
    signal, not request content, and a root span with no output and no reason is
    the thing this pipeline exists to avoid.
    """

    if not run_id:
        return
    if not capture_enabled():
        fields = {key: value for key, value in fields.items() if key == "error"}
        if not fields:
            return
    with _lock:
        entry = _entries.get(run_id)
        if entry is None:
            entry = {}
            _entries[run_id] = entry
        entry.update(fields)
        _entries.move_to_end(run_id)
        while len(_entries) > _MAX_PENDING_ENTRIES:
            dropped, _ = _entries.popitem(last=False)
            logger.debug("Dropped unclaimed trace content for workflow run %s", dropped)


def pop(run_id: str | None) -> dict[str, Any] | None:
    """Take (and release) the recorded trace content for one workflow run."""

    if not run_id:
        return None
    with _lock:
        return _entries.pop(run_id, None)


def pending_count() -> int:
    """Number of unclaimed entries. Exposed for tests."""

    with _lock:
        return len(_entries)


def clear() -> None:
    """Drop every unclaimed entry. Exposed for tests."""

    with _lock:
        _entries.clear()


class StreamTextAccumulator:
    """Accumulate streamed assistant text without delaying the stream.

    Text is appended after each chunk has already been yielded, and accumulation
    stops at the configured character limit so a very long answer bounds the
    trace instead of the trace bounding memory.
    """

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._length = 0
        self._limit = max_chars()
        self.truncated = False

    def add(self, text: str) -> None:
        if not text:
            return
        remaining = self._limit - self._length
        if remaining <= 0:
            self.truncated = True
            return
        self._parts.append(text[:remaining])
        self._length += min(len(text), remaining)
        if len(text) > remaining:
            self.truncated = True

    @property
    def text(self) -> str:
        value = "".join(self._parts)
        if self.truncated:
            return value + TRUNCATION_NOTICE
        return value


def current_run_id() -> str | None:
    """NAT's workflow run id for the request currently being served."""

    from nat.builder.context import ContextState

    try:
        return ContextState.get().workflow_run_id.get()
    except (AttributeError, LookupError):  # pragma: no cover - defensive
        return None
