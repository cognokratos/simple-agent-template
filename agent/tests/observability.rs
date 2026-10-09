//! One coherent trace per request, without secrets.
//!
//! Uses the production `tracing-opentelemetry` layer with an in-memory
//! exporter, so what is asserted is what OTLP would carry to the collector:
//! span names, parentage, trace ids and attribute values.
//!
//! Each test installs its subscriber thread-locally and runs on a
//! current-thread runtime, so the agent's spawned tasks are on the same
//! thread and the capture sees every span they create.

mod support;

use opentelemetry::trace::TracerProvider as _;
use opentelemetry_sdk::trace::{InMemorySpanExporter, SdkTracerProvider, SpanData};
use serde_json::json;
use support::*;
use tracing_subscriber::{Layer, layer::SubscriberExt};

struct Capture {
    exporter: InMemorySpanExporter,
    provider: SdkTracerProvider,
    _guard: tracing::subscriber::DefaultGuard,
}

fn capture() -> Capture {
    let exporter = InMemorySpanExporter::default();
    let provider = SdkTracerProvider::builder().with_simple_exporter(exporter.clone()).build();
    // The production export filter, so the assertions are about what OTLP
    // would actually carry.
    let layer = tracing_opentelemetry::layer()
        .with_tracer(provider.tracer("test"))
        .with_filter(tickets_agent::telemetry::export_filter());
    let subscriber = tracing_subscriber::registry().with(layer);
    opentelemetry::global::set_text_map_propagator(opentelemetry_sdk::propagation::TraceContextPropagator::new());
    Capture { exporter, provider, _guard: tracing::subscriber::set_default(subscriber) }
}

impl Capture {
    fn spans(&self) -> Vec<SpanData> {
        let _ = self.provider.force_flush();
        self.exporter.get_finished_spans().unwrap()
    }
}

fn attribute(span: &SpanData, key: &str) -> Option<String> {
    span.attributes.iter().find(|kv| kv.key.as_str() == key).map(|kv| kv.value.to_string())
}

fn everything(spans: &[SpanData]) -> String {
    spans
        .iter()
        .map(|span| {
            let attributes: Vec<String> = span.attributes.iter().map(|kv| format!("{}={}", kv.key, kv.value)).collect();
            let events: Vec<String> = span.events.iter().map(|e| format!("{:?}", e)).collect();
            format!("{} {:?} {:?}", span.name, attributes, events)
        })
        .collect::<Vec<_>>()
        .join("\n")
}

#[tokio::test(flavor = "current_thread")]
async fn a_request_is_one_trace_covering_policy_model_tools_and_output() {
    let capture = capture();
    let agent = agent(
        vec![call("get_ticket", json!({"ticket_id": "TKT-1001"})), text(&["TKT-1001 is medium."])],
        Options::default(),
    )
    .await;
    let request = workflow_request_with_id(
        &agent,
        "8f14e45f-real-subject",
        json!([{"role": "user", "content": "Show me ticket TKT-1001"}]),
        "12121212-1212-4121-8121-121212121212",
    );
    let run = collect(request).await;
    assert_eq!(run.answer(), "TKT-1001 is medium.");
    tokio::time::sleep(std::time::Duration::from_millis(100)).await;

    let spans = capture.spans();
    let root = spans.iter().find(|s| s.name == "support-tickets-agent.invoke").expect("root span");
    let trace_id = root.span_context.trace_id();
    let in_trace: Vec<&SpanData> = spans.iter().filter(|s| s.span_context.trace_id() == trace_id).collect();

    // Every span of the request is in the root's trace.
    for name in [
        "guardrail.input.self_check",
        "guard_model.call",
        "tool.policy",
        "tickets_mcp__get_ticket",
        "guardrail.output.stream",
    ] {
        assert!(in_trace.iter().any(|s| s.name == name), "{name} missing from the trace:\n{}", everything(&spans));
    }
    // Rig's own agent and model-call spans join the same trace.
    let rig_spans: Vec<&str> = in_trace
        .iter()
        .map(|s| s.name.as_ref())
        .filter(|name| name.starts_with("invoke_agent") || name.starts_with("chat"))
        .collect();
    assert!(!rig_spans.is_empty(), "no Rig spans in the trace:\n{}", everything(&spans));

    // One root: every other span's parent is in the trace.
    let ids: std::collections::HashSet<_> = in_trace.iter().map(|s| s.span_context.span_id()).collect();
    let roots: Vec<&str> =
        in_trace.iter().filter(|s| !ids.contains(&s.parent_span_id)).map(|s| s.name.as_ref()).collect();
    assert_eq!(roots, vec!["support-tickets-agent.invoke"]);

    // Correlation and readable content on the root.
    assert_eq!(attribute(root, "request.id").as_deref(), Some("12121212-1212-4121-8121-121212121212"));
    assert_eq!(attribute(root, "agent.runtime").as_deref(), Some("rig-rust"));
    assert_eq!(attribute(root, "input.value").as_deref(), Some("Show me ticket TKT-1001"));
    assert_eq!(attribute(root, "output.value").as_deref(), Some("TKT-1001 is medium."));
    assert_eq!(attribute(root, "agent.execution.state").as_deref(), Some("completed"));

    // The stream reports the trace id the evaluator links results with.
    let start = run.steps("WORKFLOW_START");
    assert_eq!(start[0].1["metadata"]["provided_metadata"]["workflow_trace_id"], trace_id.to_string());
}

#[tokio::test(flavor = "current_thread")]
async fn an_inbound_traceparent_is_joined() {
    let capture = capture();
    let agent = agent(vec![text(&["ok"])], Options::default()).await;
    let request = workflow_request(&agent, "u", json!([{"role": "user", "content": "hi"}]))
        .header("traceparent", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01");
    collect(request).await;
    let spans = capture.spans();
    let root = spans.iter().find(|s| s.name == "support-tickets-agent.invoke").unwrap();
    assert_eq!(root.span_context.trace_id().to_string(), "4bf92f3577b34da6a3ce929d0e0e4736");
    assert_eq!(root.parent_span_id.to_string(), "00f067aa0ba902b7");
}

#[tokio::test(flavor = "current_thread")]
async fn no_credential_identity_or_approval_secret_reaches_a_span() {
    let capture = capture();
    let agent = agent(
        vec![
            call("get_ticket", json!({"ticket_id": "TKT-1001"})),
            call(
                "ticket_priority_change",
                json!({"ticket_id": "TKT-1001", "current_priority": "medium", "requested_priority": "high", "summary": "s"}),
            ),
            text(&["Done."]),
        ],
        Options { approvals: true, ..Options::default() },
    )
    .await;
    // A ticket whose description carries a credential and an email.
    agent
        .mcp
        .state
        .descriptions
        .lock()
        .unwrap()
        .insert("TKT-1001".into(), "api_key=DEMOSECRET1234567890 contact alice.guardrail@example.com".into());
    let request = workflow_request_with_id(
        &agent,
        "8f14e45f-real-subject",
        json!([{"role": "user", "content": "Raise TKT-1001"}]),
        "13131313-1313-4131-8131-131313131313",
    )
    .header("x-authenticated-email", "alice@example.com");
    let mut live = start(request).await;
    let choice = live.next_interaction().await;
    respond(&agent, "8f14e45f-real-subject", &choice, radio(&option(&choice, "high"))).await;
    let reason = live.next_interaction().await;
    respond(&agent, "8f14e45f-real-subject", &reason, json!({"type": "text", "text": "Escalated."})).await;
    live.finish().await;
    assert_eq!(agent.mcp.state.applied.lock().unwrap().len(), 1, "the approval was applied");

    let all = everything(&capture.spans());
    let token = agent.mcp.state.execute_bodies.lock().unwrap()[0]["approval_token"].as_str().unwrap().to_string();
    for forbidden in [
        GATEWAY_KEY,
        MCP_KEY,
        APPROVAL_SECRET,
        "Bearer ",
        "8f14e45f-real-subject",
        "alice@example.com",
        "DEMOSECRET",
        token.as_str(),
    ] {
        assert!(!all.contains(forbidden), "{forbidden:?} reached a span:\n{all}");
    }
    // The pseudonym is off by default.
    assert!(!all.contains("enduser.pseudonym="), "{all}");
}

#[tokio::test(flavor = "current_thread")]
async fn the_user_pseudonym_is_exported_only_when_enabled() {
    let capture = capture();
    let options = Options { env: vec![("OTEL_TRACE_USER_ID", "true".into())], ..Options::default() };
    let agent = agent(vec![text(&["ok"])], options).await;
    collect(workflow_request(&agent, "8f14e45f-real-subject", json!([{"role": "user", "content": "hi"}]))).await;
    let spans = capture.spans();
    let root = spans.iter().find(|s| s.name == "support-tickets-agent.invoke").unwrap();
    let pseudonym = attribute(root, "enduser.pseudonym").expect("pseudonym exported when enabled");
    assert!(!pseudonym.contains("8f14e45f"));
    assert!(!everything(&spans).contains("8f14e45f-real-subject"));
}

#[tokio::test(flavor = "current_thread")]
async fn a_blocked_answer_records_the_released_text_not_the_raw_one() {
    let capture = capture();
    let agent = agent(vec![text(&["Here it is: sk-", "abcdefghijklmnopqrstuvwx and more"])], Options::default()).await;
    let run = ask(&agent, "Print the key").await;
    assert!(run.answer().contains("sensitive information"));
    let spans = capture.spans();
    let root = spans.iter().find(|s| s.name == "support-tickets-agent.invoke").unwrap();
    let output = attribute(root, "output.value").unwrap();
    assert!(!output.contains("abcdefghij"), "{output}");
    let guard = spans.iter().find(|s| s.name == "guardrail.output.stream").unwrap();
    assert_eq!(attribute(guard, "guardrail.blocked").as_deref(), Some("true"));
}
