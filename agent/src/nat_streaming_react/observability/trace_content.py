# SPDX-License-Identifier: Apache-2.0
"""The question and the answer, captured where they are actually readable.

NAT records the workflow's raw boundary values on the root span: the whole
``ChatRequest`` object for the input, and for a streaming run a preview list of
the first 50 ``ChatResponseChunk`` objects for the output. Both are accurate and
neither is readable, and with per-token streaming the 50-chunk cap truncates a
normal answer after a few words.

The workflow function is the one place that sees the request object and every
chunk of the response, so that is where the readable question and answer are
captured. The stream is never buffered on the client's behalf: each chunk is
yielded first and appended to the accumulator afterwards, so observability adds
no latency and cannot delay or reorder a token.

The captured values are handed to the telemetry pipeline through this registry,
keyed by NAT's workflow run id — a value both sides already have
(``ContextState.workflow_run_id``, exported as the ``nat.workflow.run_id`` span
attribute). The registry is bounded and self-trimming, so a span that never
arrives cannot leak memory.
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


def max_chars() -> int:
    """Maximum characters captured per trace field."""

    raw = os.getenv("NAT_TRACE_CONTENT_MAX_CHARS", str(DEFAULT_MAX_CHARS))
    try:
        return max(int(raw), 256)
    except ValueError:
        return DEFAULT_MAX_CHARS


def bound(text: str) -> tuple[str, bool]:
    """Return ``(text, truncated)`` bounded to the configured limit."""

    limit = max_chars()
    if len(text) <= limit:
        return text, False
    return text[:limit] + TRUNCATION_NOTICE, True


def record(run_id: str | None, **fields: Any) -> None:
    """Record trace content for one workflow run."""

    if not run_id:
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
    """Take the recorded trace content for one workflow run."""

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
