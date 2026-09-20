# SPDX-License-Identifier: Apache-2.0
"""NAT telemetry exporter for this application's OTLP/MLflow pipeline.

``register_telemetry_exporter`` is NAT's plugin API for telemetry backends: a
config type selected by ``_type`` in ``general.telemetry.tracing`` plus a factory
that yields an exporter instance. This one is the stock
``OTLPSpanAdapterExporter`` with two of our processors inserted at the head of
its pipeline, ahead of NAT's ``Span -> OtelSpan`` conversion.

Export path, all standard OpenTelemetry from here on::

    NAT intermediate steps
        -> NAT Span
        -> WorkflowContentProcessor          (readable question/answer, bounded)
        -> SensitiveHeaderRedactionProcessor (credential deny-list)
        -> SpanToOtelProcessor / batching    (NAT built-ins, untouched)
        -> OTLP/HTTP
        -> OpenTelemetry Collector
        -> MLflow

Guardrails spans reach the same collector through the process-wide OpenTelemetry
SDK configured in ``nat_streaming_react.otel_setup``, carrying the trace and
parent ids established by ``trace_context``. Two exporters, one trace.

Select it from ``agent/config.yml``::

    general:
      telemetry:
        tracing:
          mlflow_otlp:
            _type: agent_otlp
            endpoint: ...
            project: ...
"""

import logging

from nat.builder.builder import Builder
from nat.cli.register_workflow import register_telemetry_exporter
from nat.data_models.telemetry_exporter import TelemetryExporterBaseConfig
from nat.observability.mixin.batch_config_mixin import BatchConfigMixin
from nat.observability.mixin.collector_config_mixin import CollectorConfigMixin
from pydantic import Field

logger = logging.getLogger(__name__)

#: Fallback for NAT's private span-attribute prefix. See
#: NAT_PRIVATE_API_DEPENDENCIES in this package's __init__.
DEFAULT_SPAN_PREFIX = "nat"


class AgentOtlpTelemetryExporter(
    BatchConfigMixin,
    CollectorConfigMixin,
    TelemetryExporterBaseConfig,
    name="agent_otlp",
):
    """OTLP span export with this application's span normalization and redaction."""

    resource_attributes: dict[str, str] = Field(
        default_factory=dict,
        description="Additional OpenTelemetry resource attributes for every span.",
    )


@register_telemetry_exporter(config_type=AgentOtlpTelemetryExporter)
async def agent_otlp_exporter(config: AgentOtlpTelemetryExporter, builder: Builder):
    """Build the OTLP exporter and install our span processors ahead of conversion."""

    from nat.plugins.opentelemetry import OTLPSpanAdapterExporter
    from nat.plugins.opentelemetry.otel_span_exporter import get_opentelemetry_sdk_version

    from nat_streaming_react.observability.trace_processor import SensitiveHeaderRedactionProcessor
    from nat_streaming_react.observability.trace_processor import WorkflowContentProcessor

    resource_attributes = {
        "telemetry.sdk.language": "python",
        "telemetry.sdk.name": "opentelemetry",
        "telemetry.sdk.version": get_opentelemetry_sdk_version(),
        "service.name": config.project,
        **config.resource_attributes,
    }

    exporter = OTLPSpanAdapterExporter(
        endpoint=config.endpoint,
        resource_attributes=resource_attributes,
        batch_size=config.batch_size,
        flush_interval=config.flush_interval,
        max_queue_size=config.max_queue_size,
        drop_on_overflow=config.drop_on_overflow,
        shutdown_timeout=config.shutdown_timeout,
    )

    # NAT stamps span attributes with a configurable prefix held in a private
    # attribute. Read it defensively: a rename degrades to the documented
    # default rather than crashing startup, and our processors would then simply
    # not match (reduced observability, never a failed request).
    span_prefix = getattr(exporter, "_span_prefix", DEFAULT_SPAN_PREFIX)
    if span_prefix != DEFAULT_SPAN_PREFIX:
        logger.info("Using NAT span attribute prefix %r", span_prefix)

    # Position 0/1: both must run while the item is still a NAT Span, before
    # SpanToOtelProcessor converts it.
    exporter.add_processor(
        WorkflowContentProcessor(span_prefix=span_prefix),
        name="workflow_content",
        position=0,
    )
    exporter.add_processor(
        SensitiveHeaderRedactionProcessor(span_prefix=span_prefix),
        name="sensitive_header_redaction",
        position=1,
    )
    logger.info("Agent OTLP telemetry exporter ready (endpoint=%s)", config.endpoint)

    yield exporter
