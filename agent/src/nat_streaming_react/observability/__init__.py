# SPDX-License-Identifier: Apache-2.0
"""Observability owned by this application, layered on NAT's supported extension points.

* ``trace_context``   — W3C trace context at the HTTP boundary; makes NAT's
  workflow trace the single root that NeMo Guardrails spans join.
* ``trace_content``   — the question/answer the workflow actually saw, bounded.
* ``trace_processor`` — NAT telemetry processors that normalize and redact spans.
* ``mlflow_exporter`` — the registered NAT telemetry exporter wiring it together.

Nothing here modifies an installed package.
"""
