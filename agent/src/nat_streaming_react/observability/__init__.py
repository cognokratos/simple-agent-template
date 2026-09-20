# SPDX-License-Identifier: Apache-2.0
"""Observability owned by this application, layered on NAT's extension points.

* ``trace_context``  — W3C trace context at the HTTP boundary; makes NAT's
  workflow trace the single root that NeMo Guardrails spans join.
* ``trace_content``  — the question/answer the workflow actually saw, bounded.
* ``trace_processor``— NAT telemetry processors that normalize and redact spans.
* ``otlp_exporter``  — the registered NAT telemetry exporter wiring it together.

Nothing here modifies an installed package. Some of it does depend on private
NAT attributes; every such dependency is named in ``NAT_PRIVATE_API_DEPENDENCIES``
below and repeated at its use site, so an upgrade has one place to check.
"""

#: Private NAT/OpenTelemetry attributes this package reads, and why.
#:
#: These are *not* part of NAT's public API. They are used because NAT exposes no
#: public equivalent in 1.8.0, and each one is read defensively so that a rename
#: degrades to reduced observability rather than a failed request.
#:
#: ``ContextState._root_span_id``
#:     Pre-seeds the root span id so NAT's exporter and the process-wide
#:     OpenTelemetry SDK agree on one trace. NAT's own evaluation runtime
#:     (``nat/plugins/eval/runtime/evaluate.py``) sets it the same way.
#:     Removal condition: NAT exposes a public way to supply the root span id,
#:     or accepts an ambient OpenTelemetry context for the workflow root.
#:
#: ``nat.data_models.span._generate_nonzero_span_id``
#:     Generates a span id in exactly NAT's format. Removal condition: NAT
#:     exports this (or an equivalent) without the leading underscore.
#:
#: ``OtelSpanExporter._span_prefix``
#:     The attribute-name prefix ("nat") that NAT's span exporter stamps onto
#:     every span. Our processors must read the same prefixed keys. Read through
#:     ``getattr(..., "nat")`` so a rename falls back to the documented default.
#:     Removal condition: NAT exposes the prefix publicly.
NAT_PRIVATE_API_DEPENDENCIES: tuple[str, ...] = (
    "nat.builder.context.ContextState._root_span_id",
    "nat.data_models.span._generate_nonzero_span_id",
    "nat.plugins.opentelemetry.otel_span_exporter.OtelSpanExporter._span_prefix",
)
