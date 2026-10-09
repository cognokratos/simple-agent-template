//! Tracing and OpenTelemetry.
//!
//! One trace per request: the root span is opened by the workflow route
//! (`support-tickets-agent.invoke`), and everything below it — the input rail
//! and its classifier call, Rig's own `chat`/`execute_tool` spans, every tool
//! policy decision, MCP call, approval wait and the output rail — is a child
//! of it, because each is created while that span is current. An inbound W3C
//! `traceparent` is honoured, so an instrumented caller's trace is joined.
//!
//! Exported over OTLP/HTTP to the same collector as on `main`. What never
//! reaches a span: the service credentials (stripped before any handler
//! runs), the raw user subject (only a pseudonym, and only with
//! `OTEL_TRACE_USER_ID=true`), approval tokens and secrets, and — by default —
//! raw pre-output-rail model text.

pub mod provenance;

use opentelemetry::{KeyValue, global, trace::TracerProvider as _};
use opentelemetry_otlp::{Protocol, WithExportConfig};
use opentelemetry_sdk::{Resource, propagation::TraceContextPropagator, trace::SdkTracerProvider};
use tracing_opentelemetry::OpenTelemetrySpanExt;
use tracing_subscriber::{EnvFilter, Layer, layer::SubscriberExt, util::SubscriberInitExt};

use crate::config::TelemetrySettings;

/// Keeps the exporter alive; flushes on drop.
pub struct TelemetryGuard(Option<SdkTracerProvider>);

impl Drop for TelemetryGuard {
    fn drop(&mut self) {
        if let Some(provider) = self.0.take()
            && let Err(error) = provider.shutdown()
        {
            eprintln!("telemetry shutdown failed: {error}");
        }
    }
}

/// What is exported, fixed in code and deliberately *not* governed by
/// `RUST_LOG`: this service and Rig at `info`, everything else at `warn`.
///
/// Pinning Rig to `info` is a redaction control, not just noise reduction:
/// Rig logs the complete provider request — system prompt, conversation and
/// every tool result — as a TRACE event on target `rig::completions`. Raising
/// `RUST_LOG` can put that in the container log; it can never put it in the
/// exported trace.
pub const EXPORT_FILTER: &str = "warn,tickets_agent=info,rig=info,rig_agent=info,rig_core=info,rig_rmcp=info";

pub fn export_filter() -> EnvFilter {
    EnvFilter::new(EXPORT_FILTER)
}

pub fn init(settings: &TelemetrySettings) -> TelemetryGuard {
    global::set_text_map_propagator(TraceContextPropagator::new());
    let log_filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("tickets_agent=info,warn"));
    let fmt = tracing_subscriber::fmt::layer().with_target(true).with_filter(log_filter);

    let Some(endpoint) = settings.traces_endpoint.clone() else {
        tracing_subscriber::registry().with(fmt).init();
        tracing::info!("no OTLP endpoint configured; traces are not exported");
        return TelemetryGuard(None);
    };

    let exporter = match opentelemetry_otlp::SpanExporter::builder()
        .with_http()
        .with_protocol(Protocol::HttpBinary)
        .with_endpoint(endpoint.clone())
        .build()
    {
        Ok(exporter) => exporter,
        Err(error) => {
            tracing_subscriber::registry().with(fmt).init();
            tracing::error!(%error, "could not build the OTLP exporter; traces are not exported");
            return TelemetryGuard(None);
        }
    };
    let resource = Resource::builder()
        .with_service_name(settings.service_name.clone())
        .with_attributes([
            KeyValue::new("service.namespace", "rig-rust-agent"),
            KeyValue::new("deployment.environment", settings.deployment_environment.clone()),
            KeyValue::new("agent.runtime", provenance::AGENT_RUNTIME),
        ])
        .build();
    let provider = SdkTracerProvider::builder().with_batch_exporter(exporter).with_resource(resource).build();
    let tracer = provider.tracer("tickets-agent");
    global::set_tracer_provider(provider.clone());

    let otel = tracing_opentelemetry::layer().with_tracer(tracer).with_filter(export_filter());
    tracing_subscriber::registry().with(fmt).with(otel).init();
    tracing::info!(%endpoint, "exporting traces over OTLP/HTTP");
    TelemetryGuard(Some(provider))
}

/// Attach an inbound W3C trace context, if the caller sent one.
pub fn adopt_parent(span: &tracing::Span, headers: &axum::http::HeaderMap) {
    struct Extractor<'a>(&'a axum::http::HeaderMap);
    impl opentelemetry::propagation::Extractor for Extractor<'_> {
        fn get(&self, key: &str) -> Option<&str> {
            self.0.get(key).and_then(|value| value.to_str().ok())
        }
        fn keys(&self) -> Vec<&str> {
            self.0.keys().map(|key| key.as_str()).collect()
        }
    }
    let parent = global::get_text_map_propagator(|propagator| propagator.extract(&Extractor(headers)));
    let _ = span.set_parent(parent);
}

/// The current span's trace id in hex, for correlating the stream with the trace.
pub fn current_trace_id() -> String {
    use opentelemetry::trace::TraceContextExt;
    let context = tracing::Span::current().context();
    let span = context.span();
    let id = span.span_context().trace_id();
    if id == opentelemetry::trace::TraceId::INVALID { String::new() } else { id.to_string() }
}

/// Bound a text attribute, marking truncation as `main` does.
pub fn bounded(text: &str, limit: usize) -> String {
    if text.chars().count() <= limit {
        text.to_string()
    } else {
        let cut: String = text.chars().take(limit).collect();
        format!("{cut}\n[trace content truncated]")
    }
}
