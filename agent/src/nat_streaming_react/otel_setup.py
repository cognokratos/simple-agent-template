# SPDX-License-Identifier: Apache-2.0
"""Configure the process-wide OpenTelemetry SDK used by Guardrails child spans.

NAT exports the canonical workflow/tool tree through its own exporter. The
patched NAT runner installs a matching non-recording parent context so standard
OpenTelemetry spans created here join that same trace.
"""

import logging
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)
_configured = False


def configure_opentelemetry() -> None:
    """Install one global OTLP/HTTP tracer provider if none was configured."""

    global _configured
    if _configured:
        return

    current = trace.get_tracer_provider()
    if isinstance(current, TracerProvider):
        _configured = True
        return

    endpoint = os.getenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        os.getenv(
            "OTEL_COLLECTOR_TRACES_ENDPOINT",
            "http://otel-collector:4318/v1/traces",
        ),
    )
    service_name = os.getenv("OTEL_SERVICE_NAME", "alerts-agent")
    environment = os.getenv("DEPLOYMENT_ENVIRONMENT", "local")

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service_name,
                "service.namespace": "nemo-agent-toolkit",
                "deployment.environment": environment,
            }
        )
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint),
            max_queue_size=2048,
            max_export_batch_size=256,
            schedule_delay_millis=1000,
        )
    )
    trace.set_tracer_provider(provider)
    _configured = True
    logger.info("Configured OpenTelemetry OTLP/HTTP export to %s", endpoint)
