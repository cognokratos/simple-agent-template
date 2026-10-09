//! One request, start to finish.
//!
//! This is the file to read to follow the agent lifecycle. Each numbered step
//! says whether the decision made there is **probabilistic** (the model) or
//! **deterministic** (this service):
//!
//! ```text
//! 1. trusted request        deterministic   api::auth built the TrustedCaller
//! 2. input policy           both            guardrails::input + one classifier call
//! 3. agent loop             Rig             model turn ⇄ tool calls, bounded
//!    3a. tool decision      probabilistic   the model proposes a call
//!    3b. tool policy        deterministic   agent::hooks → guardrails::tools
//!    3c. MCP invocation     deterministic   mcp::tools → MCP server (authoritative)
//!    3d. approval gate      deterministic   approval::gate, suspends on a human
//! 4. output policy          deterministic   guardrails::output, while streaming
//! 5. streamed response      deterministic   api::wire
//! ```

use std::sync::Arc;

use futures::StreamExt;
use rig_agent::{agent::MultiTurnStreamItem, completion::PromptError};
use rig_core::{
    completion::Message,
    streaming::{Item, StreamEvent},
};
use serde_json::json;

use super::{
    builder::build_agent,
    hooks::ToolPolicyHook,
    input_rail::{self, InputOutcome},
    scope::RequestScope,
    state::Transition,
};
use crate::{
    api::wire::{Step, StepType, WireEvent},
    guardrails::{
        input::{Role, Turn},
        output::{GuardState, GuardStep},
    },
    telemetry::bounded,
};

pub const OUTPUT_DECISION_EVENT: &str = "guardrail_output_regex_pii_decision";

/// The conversation as received: validated, trimmed, last turn from the user.
pub struct Conversation {
    pub history: Vec<Turn>,
    pub latest: String,
}

/// Records how the run ended even when the task is aborted (client
/// disconnect): dropping a non-terminal run is a cancellation.
struct CancelOnDrop {
    scope: Arc<RequestScope>,
    root: tracing::Span,
}

impl Drop for CancelOnDrop {
    fn drop(&mut self) {
        if !self.scope.state().is_terminal() {
            self.scope.transition(Transition::Cancel);
            self.root.record("agent.execution.state", "cancelled");
            tracing::info!(parent: &self.root, "run cancelled: the client went away");
        }
    }
}

pub async fn run(scope: Arc<RequestScope>, conversation: Conversation) {
    let root = tracing::Span::current();
    let _cancel = CancelOnDrop { scope: Arc::clone(&scope), root: root.clone() };
    let services = Arc::clone(scope.services());
    let telemetry = &services.settings.telemetry;
    let workflow_name = services.settings.file.workflow.name.clone();
    let events = scope.events();

    // Trace correlation the evaluator reads from the stream.
    let trace_id = crate::telemetry::current_trace_id();
    let metadata = json!({
        "workflow_trace_id": trace_id,
        "workflow_run_id": scope.execution_id(),
        "request_id": scope.caller().request_id(),
        "agent_runtime": "rig-rust",
    });
    events
        .step(
            Step::new(StepType::WorkflowStart, scope.workflow_step_id(), &workflow_name)
                .with_metadata(metadata.clone()),
        )
        .await;
    if telemetry.capture_content {
        root.record("input.value", bounded(&conversation.latest, telemetry.content_max_chars).as_str());
    }

    // ── 2. Input policy ────────────────────────────────────────────────────
    scope.transition(Transition::ScreenInput);
    if let InputOutcome::Refused(message) =
        input_rail::screen(&scope, &conversation.latest, &conversation.history).await
    {
        scope.transition(Transition::Block);
        events.emit(WireEvent::Answer(message.clone())).await;
        finish(&scope, &root, &workflow_name, metadata, &message, "blocked").await;
        return;
    }
    scope.transition(Transition::Allow);

    // ── 3. The agent loop, owned by Rig ────────────────────────────────────
    let agent = build_agent(&scope);
    let history: Vec<Message> = conversation
        .history
        .iter()
        .map(|turn| match turn.role {
            Role::User => Message::user(turn.content.clone()),
            Role::Assistant => Message::assistant(turn.content.clone()),
        })
        .collect();
    let max_turns = services.settings.file.workflow.max_tool_calls + 1;
    let mut stream = agent
        .prompt(conversation.latest.clone())
        .history(history)
        .max_turns(max_turns)
        // 3b: every tool call the model proposes passes through this hook.
        .add_hook(ToolPolicyHook::new(Arc::clone(&scope)))
        .stream();

    // ── 4. Output policy, applied while streaming ──────────────────────────
    let mut guard = services.output_policy.guard();
    let mut released = String::new();
    let mut failure: Option<String> = None;
    let output_span = tracing::info_span!(
        "guardrail.output.stream",
        openinference.span.kind = "GUARDRAIL",
        guardrail.stage = "output",
        guardrail.name = "regex + deterministic PII output",
        guardrail.type = "streaming_window_output_pipeline",
        guardrail.outcome = tracing::field::Empty,
        guardrail.blocked = tracing::field::Empty,
        guardrail.modified = tracing::field::Empty,
        guardrail.pattern_index = tracing::field::Empty,
        guardrail.masked_entities = tracing::field::Empty,
        guardrail.input.length = tracing::field::Empty,
        guardrail.output.length = tracing::field::Empty,
    );

    while let Some(item) = stream.next().await {
        let text = match item {
            // Only assistant *text* is ever a candidate for release. Reasoning
            // fragments and tool-call arguments are never streamed to the client.
            Ok(MultiTurnStreamItem::StreamAssistantItem(Item::Event(StreamEvent::Text { text, .. }))) => text,
            Ok(_) => continue,
            Err(PromptError::MaxTurns { .. }) => {
                // The same bounded-loop answer as `main`, after what was released.
                format!(
                    "The agent could not produce a final answer within {} tool calls.",
                    services.settings.file.workflow.max_tool_calls
                )
            }
            Err(error) => {
                // Logged in full, reported without internals.
                tracing::error!(%error, "agent run failed");
                failure = Some(public_failure(&error));
                break;
            }
        };
        match output_span.in_scope(|| guard.push(&text)) {
            GuardStep::Release(text) => {
                released.push_str(&text);
                events.emit(WireEvent::Answer(text)).await;
            }
            GuardStep::Hold => {}
            GuardStep::Stop(notice) => {
                released.push_str(&notice);
                events.emit(WireEvent::Answer(notice)).await;
                // Dropping the stream cancels the rest of the run, including
                // any tool call that has not started.
                break;
            }
        }
    }
    drop(stream);
    if guard.state() == GuardState::Streaming {
        match guard.finish() {
            GuardStep::Release(text) | GuardStep::Stop(text) if !text.is_empty() => {
                released.push_str(&text);
                events.emit(WireEvent::Answer(text)).await;
            }
            _ => {}
        }
    }

    let stats = guard.stats().clone();
    let blocked = matches!(guard.state(), GuardState::Blocked | GuardState::Oversized);
    let modified = !stats.masked_entities.is_empty();
    let outcome = if blocked {
        "blocked"
    } else if modified {
        "modified"
    } else {
        "passed"
    };
    output_span.record("guardrail.outcome", outcome);
    output_span.record("guardrail.blocked", blocked);
    output_span.record("guardrail.modified", modified);
    output_span.record("guardrail.input.length", stats.raw_chars);
    output_span.record("guardrail.output.length", stats.released_chars);
    if let Some(index) = stats.blocked_by_pattern {
        output_span.record("guardrail.pattern_index", index);
    }
    output_span.record(
        "guardrail.masked_entities",
        serde_json::to_string(&stats.masked_entities).unwrap_or_default().as_str(),
    );
    drop(output_span);

    let decision_id = uuid::Uuid::new_v4().to_string();
    events
        .step(
            Step::new(StepType::FunctionEnd, &decision_id, OUTPUT_DECISION_EVENT)
                .parent(scope.workflow_step_id())
                .with_output(
                    json!({ "raw_output_length": stats.raw_chars }),
                    json!({
                        "stage": "output",
                        "name": "regex + deterministic PII output",
                        "outcome": outcome,
                        "blocked": blocked,
                        "modified": modified,
                        "oversized": guard.state() == GuardState::Oversized,
                        "masked_entities": stats.masked_entities,
                        "sanitized_output_length": stats.released_chars,
                    }),
                ),
        )
        .await;

    // ── 5. End of stream ───────────────────────────────────────────────────
    if let Some(message) = failure {
        scope.transition(Transition::Fail);
        root.record("error.message", message.as_str());
        events.emit(WireEvent::Error { message }).await;
        finish(&scope, &root, &workflow_name, metadata, &released, "failed").await;
        return;
    }
    scope.transition(Transition::Complete);
    finish(&scope, &root, &workflow_name, metadata, &released, scope.state().name()).await;
}

async fn finish(
    scope: &RequestScope,
    root: &tracing::Span,
    workflow_name: &str,
    metadata: serde_json::Value,
    answer: &str,
    state: &str,
) {
    let telemetry = &scope.services().settings.telemetry;
    if telemetry.capture_content {
        // The *released* answer: masked and cut exactly as the client saw it,
        // never the raw model text.
        root.record("output.value", bounded(answer, telemetry.content_max_chars).as_str());
    }
    root.record("agent.execution.state", state);
    scope
        .events()
        .step(Step::new(StepType::WorkflowEnd, scope.workflow_step_id(), workflow_name).with_metadata(metadata))
        .await;
}

/// A failure description safe to send to the client: the class, not the
/// provider's message, which can carry URLs and internal hostnames.
fn public_failure(error: &PromptError) -> String {
    use rig_core::error::ErrorKind;
    let provider = match error {
        PromptError::Provider(_) => true,
        PromptError::Report(report) => matches!(
            report.kind,
            ErrorKind::Http | ErrorKind::Provider | ErrorKind::ProviderResponse | ErrorKind::Response | ErrorKind::Json
        ),
        _ => false,
    };
    if provider { "The model provider failed to answer.".into() } else { "The agent run failed.".into() }
}
