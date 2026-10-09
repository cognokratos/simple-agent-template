//! Running the input policy for one request.
//!
//! `guardrails::input` holds the rules as pure functions; this runs them in
//! order, makes the one model call, and reports the decision on the stream
//! (`guardrail_input_self_check_decision`, which the evaluator scores) and on
//! the trace (`guardrail.input.self_check`), with `main`'s field names.

use serde_json::json;
use sha2::{Digest, Sha256};

use super::{model, scope::RequestScope};
use crate::{
    api::wire::{Step, StepType},
    guardrails::input::{self, DecisionSource, InputDecision, Turn},
    telemetry::bounded,
};

/// NeMo's refusal when its self-check input rail blocks.
pub const CLASSIFIER_REFUSAL: &str = "I'm sorry, I can't respond to that.";
pub const DECISION_EVENT: &str = "guardrail_input_self_check_decision";

pub enum InputOutcome {
    Allowed,
    /// Refused; this text is the whole answer, and the agent model is never called.
    Refused(String),
}

fn sha256_hex(text: &str) -> String {
    Sha256::digest(text.as_bytes()).iter().map(|byte| format!("{byte:02x}")).collect()
}

pub async fn screen(scope: &RequestScope, latest: &str, history: &[Turn]) -> InputOutcome {
    let services = scope.services();
    let settings = &services.settings.guardrails;
    let span = tracing::info_span!(
        "guardrail.input.self_check",
        openinference.span.kind = "GUARDRAIL",
        guardrail.stage = "input",
        guardrail.name = "self check input",
        guardrail.framework = "rig-rust",
        guardrail.type = "llm_self_check_with_deny_and_read_only_allow_overrides",
        guardrail.input.sha256 = %sha256_hex(latest),
        guardrail.input.length = latest.chars().count(),
        guardrail.outcome = tracing::field::Empty,
        guardrail.blocked = tracing::field::Empty,
        guardrail.llm.blocked = tracing::field::Empty,
        guardrail.final.blocked = tracing::field::Empty,
        guardrail.decision_source = tracing::field::Empty,
        guardrail.deterministic.matches = tracing::field::Empty,
        guardrail.deterministic.allow_matches = tracing::field::Empty,
        guardrail.deterministic.allow_override_applied = tracing::field::Empty,
        guardrail.user_input = tracing::field::Empty,
        guardrail.llm.response = tracing::field::Empty,
        gen_ai.operation.name = "guardrail_check",
        gen_ai.request.model = %services.settings.guard.model,
    );
    if settings.trace_capture_content {
        span.record("guardrail.user_input", bounded(latest, settings.trace_max_chars).as_str());
    }

    let step_id = uuid::Uuid::new_v4().to_string();
    let events = scope.events();
    events
        .step(Step::new(StepType::FunctionStart, &step_id, DECISION_EVENT).parent(scope.workflow_step_id()).with_input(
            json!({ "user_message_sha256": sha256_hex(latest), "user_message_length": latest.chars().count() }),
        ))
        .await;

    // 1. Bounded before the classifier is asked anything. Refused, never
    //    truncated: a verdict about a prefix is not a verdict about the request.
    let length = latest.chars().count();
    if length > settings.input_max_chars {
        span.record("guardrail.outcome", "blocked");
        span.record("guardrail.blocked", true);
        span.record("guardrail.final.blocked", true);
        span.record("guardrail.decision_source", "deterministic_input_limit");
        let decision = json!({
            "stage": "input", "outcome": "blocked", "blocked": true,
            "decision_source": DecisionSource::DeterministicInputLimit,
            "input_length": length, "input_limit": settings.input_max_chars,
        });
        events
            .step(
                Step::new(StepType::FunctionEnd, &step_id, DECISION_EVENT)
                    .parent(scope.workflow_step_id())
                    .with_output(json!({}), decision),
            )
            .await;
        tracing::warn!(parent: &span, length, limit = settings.input_max_chars, "refused an oversized user message");
        return InputOutcome::Refused(settings.oversize_message.clone());
    }

    // 2. Deterministic layers: pure functions, no model.
    let screened = input::screen(latest, history, settings.deterministic_fallback, settings.read_only_allow_override);

    // 3. The probabilistic layer: a separate classifier call. Its *reply* is
    //    probabilistic; how the reply is read is not (fail closed).
    let prompt_template = &services.settings.file.self_check_prompt().expect("validated at startup");
    let rendered = input::render_self_check(&prompt_template.content, latest);
    let classifier = tracing::Instrument::instrument(
        model::classify(&services.models.guard, &services.settings.guard, rendered, prompt_template.max_tokens),
        tracing::info_span!(parent: &span, "guard_model.call", gen_ai.operation.name = "chat", gen_ai.request.model = %services.settings.guard.model),
    )
    .await;
    let (llm_blocked, classifier_failed) = match &classifier {
        Ok(reply) => {
            if settings.trace_capture_content {
                span.record("guardrail.llm.response", bounded(reply, 256).as_str());
            }
            (!input::classifier_says_safe(reply), false)
        }
        Err(error) => {
            // Fail closed: no verdict is a block, as an unparseable one is.
            tracing::error!(parent: &span, %error, "input classifier unavailable; refusing the request");
            (true, true)
        }
    };

    // 4. Precedence, deterministically.
    let decision: InputDecision = input::resolve(llm_blocked, screened.block_matches, screened.allow_matches);
    let outcome = if decision.blocked { "blocked" } else { "passed" };
    span.record("guardrail.outcome", outcome);
    span.record("guardrail.blocked", decision.blocked);
    span.record("guardrail.llm.blocked", decision.llm_blocked);
    span.record("guardrail.final.blocked", decision.blocked);
    span.record(
        "guardrail.decision_source",
        serde_json::to_value(decision.decision_source)
            .ok()
            .and_then(|v| v.as_str().map(str::to_string))
            .unwrap_or_default()
            .as_str(),
    );
    span.record(
        "guardrail.deterministic.matches",
        serde_json::to_string(&decision.deterministic_block_matches).unwrap_or_default().as_str(),
    );
    span.record(
        "guardrail.deterministic.allow_matches",
        serde_json::to_string(&decision.deterministic_allow_matches).unwrap_or_default().as_str(),
    );
    span.record("guardrail.deterministic.allow_override_applied", decision.allow_override_applied);

    events
        .step(Step::new(StepType::FunctionEnd, &step_id, DECISION_EVENT).parent(scope.workflow_step_id()).with_output(
            json!({}),
            json!({
                "stage": "input",
                "name": "self check input",
                "outcome": outcome,
                "blocked": decision.blocked,
                "modified": false,
                "llm_blocked": decision.llm_blocked,
                "classifier_available": !classifier_failed,
                "deterministic_blocked": !decision.deterministic_block_matches.is_empty(),
                "deterministic_block_matches": decision.deterministic_block_matches,
                "deterministic_allow_matches": decision.deterministic_allow_matches,
                "allow_override_applied": decision.allow_override_applied,
                "decision_source": decision.decision_source,
                "llm_call_count": 1,
            }),
        ))
        .await;

    if !decision.blocked {
        return InputOutcome::Allowed;
    }
    // The classifier's own refusal whenever it voted to block; the configured
    // message when only a deterministic layer did — the same split as `main`.
    let message = if decision.llm_blocked && !classifier_failed {
        CLASSIFIER_REFUSAL.to_string()
    } else {
        settings.block_message.clone()
    };
    InputOutcome::Refused(message)
}
