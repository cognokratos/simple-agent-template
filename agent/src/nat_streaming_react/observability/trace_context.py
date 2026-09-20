# SPDX-License-Identifier: Apache-2.0
"""One trace per request, established at the HTTP boundary.

The problem
-----------
NAT builds its workflow/tool/LLM span tree itself from intermediate steps and
exports it through its own exporter. NeMo Guardrails emits ordinary
OpenTelemetry spans through the process-wide SDK. Left alone the two systems
pick unrelated trace IDs, so MLflow shows the agent run and the safety decisions
as two disconnected traces.

The two systems agree if, and only if, they start from the same
``(trace_id, root_span_id)`` pair. NAT lets that pair be supplied from outside
the runner, through two mechanisms:

* ``ContextState.workflow_trace_id`` — public. The runner adopts an existing
  value instead of generating one (``nat/runtime/runner.py``), and NAT's own
  session layer populates it from a W3C ``traceparent`` header
  (``nat/runtime/session.py::set_metadata_from_http_request``).
* ``ContextState._root_span_id`` — **private**. NAT's span exporter uses a
  pre-generated root span id "for eager trace linking" and then clears it so
  children get fresh ids (``nat/observability/exporter/span_exporter.py``).
  NAT's own evaluation runtime sets it this way before entering the runner
  (``nat/plugins/eval/runtime/evaluate.py``), relying on ``asyncio.create_task``
  copying context variables into the runner's task.

This module uses those two hooks from the HTTP boundary instead of rewriting
``nat.runtime.runner`` at build time, which is what this replaces. It also
installs a matching ``NonRecordingSpan`` as the ambient OpenTelemetry parent, so
every plain OTel span created during the request — Guardrails decisions above
all — becomes a child of NAT's workflow root rather than a new trace root.

``NonRecordingSpan`` is the standard OpenTelemetry type for "a span that exists
elsewhere": it carries a ``SpanContext`` for parenting but is never itself
exported. NAT exports the real root span; this is only the handle children need.

Private-API reliance
--------------------
``ContextState._root_span_id`` and ``nat.data_models.span._generate_nonzero_span_id``
are private NAT names. See ``NAT_PRIVATE_API_DEPENDENCIES`` in this package's
``__init__`` for the full list and the conditions under which each can be
dropped. Both are resolved at import time, so a NAT upgrade that renames them
fails loudly at startup rather than silently producing split traces.

Distributed tracing
-------------------
An inbound ``traceparent`` is honoured, so when a caller is instrumented the
agent joins that caller's trace instead of starting a new one. When there is no
inbound context a fresh trace is started here. Either way the request has
exactly one trace, established before NAT sees it.
"""

import logging
import secrets
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any

from nat.builder.context import ContextState

# Private NAT helper: see NAT_PRIVATE_API_DEPENDENCIES in this package's __init__.
from nat.data_models.span import _generate_nonzero_span_id
from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace
from opentelemetry.trace import INVALID_TRACE_ID
from opentelemetry.trace import NonRecordingSpan
from opentelemetry.trace import SpanContext
from opentelemetry.trace import TraceFlags
from opentelemetry.trace import TraceState
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

logger = logging.getLogger(__name__)

_PROPAGATOR = TraceContextTextMapPropagator()


def _incoming_trace_id(headers: dict[str, str]) -> int | None:
    """Return the trace id of a valid inbound W3C trace context, if any."""

    try:
        parent = otel_trace.get_current_span(_PROPAGATOR.extract(carrier=headers))
        span_context = parent.get_span_context()
    except Exception:  # pragma: no cover - a malformed header must never fail a request
        return None
    if not span_context.is_valid or span_context.trace_id == INVALID_TRACE_ID:
        return None
    return span_context.trace_id


def _generate_trace_id() -> int:
    """A random, non-zero 128-bit trace id."""

    trace_id = 0
    while trace_id == 0:
        trace_id = secrets.randbits(128)
    return trace_id


class RequestTraceScope:
    """Holds the identifiers and context tokens for one request's trace."""

    def __init__(self, trace_id: int, root_span_id: int, joined_upstream: bool) -> None:
        self.trace_id = trace_id
        self.root_span_id = root_span_id
        self.joined_upstream = joined_upstream

        context_state = ContextState.get()
        self._context_state = context_state
        self._trace_id_token = context_state.workflow_trace_id.set(trace_id)
        self._root_span_id_token = context_state._root_span_id.set(root_span_id)

        parent = NonRecordingSpan(
            SpanContext(
                trace_id=trace_id,
                span_id=root_span_id,
                is_remote=False,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
                trace_state=TraceState(),
            )
        )
        self._otel_token = otel_context.attach(otel_trace.set_span_in_context(parent))

    @property
    def traceparent(self) -> str:
        """The W3C header a downstream service would need to join this trace."""

        return f"00-{self.trace_id:032x}-{self.root_span_id:016x}-01"

    def close(self) -> None:
        """Release every context token. Safe to call exactly once."""

        # Detached in reverse order of attachment, and each step is independent
        # so one failure cannot leak the others.
        for reset in (
            lambda: otel_context.detach(self._otel_token),
            lambda: self._context_state._root_span_id.reset(self._root_span_id_token),
            lambda: self._context_state.workflow_trace_id.reset(self._trace_id_token),
        ):
            try:
                reset()
            except Exception:  # pragma: no cover - cleanup must never raise
                logger.debug("Trace context cleanup step failed", exc_info=True)


def start_request_trace(headers: dict[str, str]) -> RequestTraceScope:
    """Establish the single trace context for one inbound request."""

    upstream_trace_id = _incoming_trace_id(headers)
    trace_id = upstream_trace_id if upstream_trace_id is not None else _generate_trace_id()
    return RequestTraceScope(
        trace_id=trace_id,
        root_span_id=_generate_nonzero_span_id(),
        joined_upstream=upstream_trace_id is not None,
    )


class WorkflowTraceContextMiddleware:
    """ASGI middleware that gives every workflow request exactly one trace.

    Pure ASGI on purpose: it must not sit between NAT and the client's byte
    stream, so ``send``/``receive`` are passed straight through and the scope
    covers the whole response, streaming included. Starlette's
    ``BaseHTTPMiddleware`` would buffer every SSE token through an anyio memory
    stream.

    The scope is closed in a ``finally``, so it is released on success, on
    exception and on client disconnect alike.
    """

    def __init__(self, app: Any, *, excluded_paths: frozenset[str] = frozenset()) -> None:
        self.app = app
        self._excluded_paths = excluded_paths

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[Any]],
        send: Callable[[Any], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http" or scope.get("path") in self._excluded_paths:
            await self.app(scope, receive, send)
            return

        headers = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in scope.get("headers", ())
        }
        trace_scope = start_request_trace(headers)
        try:
            await self.app(scope, receive, send)
        finally:
            trace_scope.close()
