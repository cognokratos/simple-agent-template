//! The approval gate: what runs when the model proposes `ticket_priority_change`.
//!
//! One proposal, at most two human prompts, at most one token, at most one
//! mutation — and authorisation and effect are a single step: nothing between
//! the human's answer and the MCP call depends on further model output, so a
//! change can never end up approved but unapplied, and the model never gets a
//! chance to restate what was approved.

use serde_json::{Map, Value, json};
use tracing::Instrument;

use super::{
    ACTION, PRIORITIES,
    execute::{self, ExecuteOutcome},
    pending::{Decision, Prompt},
    token::{ApprovedChange, Claims},
};
use crate::{
    agent::scope::{AskOutcome, RequestScope},
    api::wire::OptionView,
};

/// The model's proposal, from arguments the tool policy already validated.
/// Every field here is untrusted model output; none of it is authority.
#[derive(Debug, Clone, PartialEq)]
pub struct Proposal {
    pub ticket_id: String,
    pub current_priority: String,
    pub requested_priority: String,
    pub summary: String,
    note: Option<String>,
}

impl Proposal {
    pub fn from_args(args: &Map<String, Value>) -> Result<Self, String> {
        let text = |key: &str| args.get(key).and_then(Value::as_str).map(str::trim).map(str::to_string);
        let ticket_id = text("ticket_id").filter(|id| !id.is_empty()).ok_or("ticket_id is required")?;
        let current_priority = text("current_priority").ok_or("current_priority is required")?;
        let requested_priority = text("requested_priority").ok_or("requested_priority is required")?;
        for priority in [&current_priority, &requested_priority] {
            if !PRIORITIES.contains(&priority.as_str()) {
                return Err(format!("unknown priority {priority:?}"));
            }
        }
        let summary = text("summary").filter(|s| !s.is_empty()).ok_or("summary is required")?;
        Ok(Self { ticket_id, current_priority, requested_priority, summary, note: text("note") })
    }

    /// The note as it will be shown, signed and persisted — computed exactly
    /// once. Every prompt and the signed claims read this value, never the
    /// raw argument again, so what the human read is what gets signed.
    fn note(&self) -> String {
        self.note.clone().unwrap_or_default()
    }
}

fn note_disclosure(note: &str) -> String {
    format!(
        "Model-supplied note (not verified by a human; will be signed and recorded verbatim if you approve): {note}"
    )
}

/// The first prompt's text, line for line as on `main`.
pub fn choice_text(proposal: &Proposal, note: &str) -> String {
    let mut lines = vec![
        format!("Ticket {} is currently '{}' priority.", proposal.ticket_id, proposal.current_priority),
        format!("The assistant proposes '{}'.", proposal.requested_priority),
        format!("Summary: {}", proposal.summary),
    ];
    if !note.is_empty() {
        lines.push(note_disclosure(note));
    }
    lines.push("Choose the priority to record. Any change requires a reason.".into());
    lines.join("\n")
}

/// Every priority, including the current one, so the person can decline the
/// change without abandoning the conversation. Cancel is appended by
/// [`Prompt::choice`].
pub fn priority_options(current: &str) -> Vec<OptionView> {
    PRIORITIES
        .iter()
        .map(|&priority| {
            let keep = priority == current;
            OptionView {
                id: priority.into(),
                value: priority.into(),
                label: if keep { format!("Keep — {priority}") } else { format!("Change to — {priority}") },
                description: if keep {
                    "No change is applied.".into()
                } else {
                    "Requires a reason, recorded against your identity.".into()
                },
            }
        })
        .collect()
}

fn rationale_text(proposal: &Proposal, choice: &str, note: &str) -> String {
    let mut lines = vec![
        format!("Ticket {}: {}", proposal.ticket_id, proposal.summary),
        format!(
            "The ticket is currently '{}' priority. You are changing it to '{choice}'. This is recorded in the \
             append-only audit trail against your identity and requires a reason.",
            proposal.current_priority
        ),
    ];
    if !note.is_empty() {
        lines.push(note_disclosure(note));
    }
    lines.join("\n")
}

fn cancelled(resource_id: &str, message: &str) -> Value {
    json!({ "approved": false, "resource_id": resource_id, "action": ACTION, "message": message })
}

/// What the model is told after an approved change is attempted. Conditional
/// on `committed`: an approval is not an outcome, and reporting success either
/// way is how an agent tells a user that a refused change was applied.
pub fn approval_result(resource_id: &str, request_id: &str, outcome: &ExecuteOutcome) -> Value {
    // The MCP server echoes `actor_id`. It stays out of the model's context:
    // identity is never conversation content, not even after the fact. (`main`
    // passes the MCP result through unchanged.)
    let mut result = outcome.result.clone();
    if let Some(object) = result.as_object_mut() {
        object.remove("actor_id");
    }
    json!({
        "approved": true,
        "resource_id": resource_id,
        "action": ACTION,
        "request_id": request_id,
        "committed": outcome.committed,
        "result": result,
        "next_step": if outcome.committed {
            "The change is already applied. Report the outcome."
        } else {
            "The change was REFUSED and NOTHING was applied. The record is unchanged. Report the refusal \
             and the reason given in result; never say or imply that any change occurred."
        },
    })
}

/// Run the gate for one validated proposal. Returns the tool result the model
/// sees; never an error, because every failure mode has a defined, honest
/// answer for the model ("nothing was changed").
pub async fn run(scope: &RequestScope, args: &Map<String, Value>) -> Value {
    let span = tracing::info_span!(
        "approval.gate",
        approval.action = ACTION,
        approval.resource_id = tracing::field::Empty,
        approval.outcome = tracing::field::Empty,
        approval.override = tracing::field::Empty,
        approval.committed = tracing::field::Empty,
    );
    run_in(scope, args, &span).instrument(span.clone()).await
}

async fn run_in(scope: &RequestScope, args: &Map<String, Value>, span: &tracing::Span) -> Value {
    let Some(settings) = scope.services().settings.approval.as_ref() else {
        // Unreachable when the policy is wired correctly: the tool is not
        // registered without the feature. Kept so the gate fails closed alone.
        return json!({ "approved": false, "message": "State-changing actions are disabled in this deployment." });
    };
    let proposal = match Proposal::from_args(args) {
        Ok(proposal) => proposal,
        Err(reason) => return json!({ "approved": false, "message": reason }),
    };
    span.record("approval.resource_id", proposal.ticket_id.as_str());

    // An approval must bind to the authenticated request the gateway minted.
    // A locally generated id names nothing the MCP server could check.
    let Some(request_id) = scope.caller().gateway_request_id().map(str::to_string) else {
        span.record("approval.outcome", "refused_no_gateway_request");
        return cancelled(
            &proposal.ticket_id,
            "Human approval requires an authenticated gateway request. Nothing was changed.",
        );
    };

    let note = proposal.note();

    let choice = match scope
        .ask(Prompt::choice(choice_text(&proposal, &note), priority_options(&proposal.current_priority)))
        .await
    {
        AskOutcome::Decided(Decision::Selected(choice)) => choice,
        AskOutcome::Decided(_) => {
            span.record("approval.outcome", "cancelled");
            return cancelled(&proposal.ticket_id, "The user cancelled.");
        }
        AskOutcome::TimedOut => {
            span.record("approval.outcome", "timed_out");
            return cancelled(&proposal.ticket_id, "The approval request expired. Nothing was changed.");
        }
        AskOutcome::Unavailable(reason) => {
            span.record("approval.outcome", "unavailable");
            return cancelled(&proposal.ticket_id, &reason);
        }
    };
    // The registry only returns an offered value, so this holds by
    // construction; it is checked again because the cost is one comparison.
    if !PRIORITIES.contains(&choice.as_str()) {
        span.record("approval.outcome", "refused_unknown_choice");
        return cancelled(&proposal.ticket_id, "The selected priority is not recognised. Nothing was changed.");
    }
    if choice == proposal.current_priority {
        // Keeping the current priority is a decision, not a mutation: nothing
        // is minted, so nothing can be applied.
        span.record("approval.outcome", "kept");
        return cancelled(
            &proposal.ticket_id,
            &format!("The user kept the current priority '{choice}'. Nothing was changed."),
        );
    }

    let rationale = match scope
        .ask(Prompt::text(
            rationale_text(&proposal, &choice, &note),
            "Why should this ticket's priority change?".into(),
        ))
        .await
    {
        AskOutcome::Decided(Decision::Answered(text)) => text,
        AskOutcome::Decided(_) => {
            span.record("approval.outcome", "cancelled");
            return cancelled(&proposal.ticket_id, "The user cancelled.");
        }
        AskOutcome::TimedOut => {
            span.record("approval.outcome", "timed_out");
            return cancelled(&proposal.ticket_id, "The approval request expired. Nothing was changed.");
        }
        AskOutcome::Unavailable(reason) => {
            span.record("approval.outcome", "unavailable");
            return cancelled(&proposal.ticket_id, &reason);
        }
    };

    let change = ApprovedChange {
        action: ACTION,
        resource_id: proposal.ticket_id.clone(),
        // From the gateway, never from the model.
        actor_id: scope.caller().user_id().to_string(),
        request_id: request_id.clone(),
        choice: choice.clone(),
        // The state the human was shown, as the model reported it. The MCP
        // server re-reads it under a row lock and voids the token on mismatch.
        expected_choice: proposal.current_priority.clone(),
        rationale: Some(rationale),
        note,
    };
    span.record("approval.override", change.choice != change.expected_choice);
    let claims = match Claims::for_change(&change, settings.token_ttl_seconds, None, None) {
        Ok(claims) => claims,
        Err(error) => {
            span.record("approval.outcome", "mint_refused");
            return cancelled(&proposal.ticket_id, &format!("{error}. Nothing was changed."));
        }
    };
    let token = claims.mint(&settings.secret);

    let services = scope.services();
    let outcome =
        execute::apply(&services.http, &services.settings.mcp_url, &services.settings.mcp_api_key, &token, &request_id)
            .instrument(tracing::info_span!("mcp.approvals.execute", otel.kind = "client"))
            .await;
    span.record("approval.committed", outcome.committed);
    span.record("approval.outcome", if outcome.committed { "applied" } else { "refused_by_mcp" });
    approval_result(&proposal.ticket_id, &request_id, &outcome)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(pairs: Value) -> Map<String, Value> {
        pairs.as_object().unwrap().clone()
    }

    #[test]
    fn the_prompt_text_matches_main_line_for_line() {
        let proposal = Proposal::from_args(&args(json!({
            "ticket_id": " TKT-1001 ",
            "current_priority": "medium",
            "requested_priority": "high",
            "summary": "Customer followed up twice.",
            "note": "Second follow-up on 2026-09-17."
        })))
        .unwrap();
        assert_eq!(
            choice_text(&proposal, &proposal.note()),
            "Ticket TKT-1001 is currently 'medium' priority.\n\
             The assistant proposes 'high'.\n\
             Summary: Customer followed up twice.\n\
             Model-supplied note (not verified by a human; will be signed and recorded verbatim if you approve): Second follow-up on 2026-09-17.\n\
             Choose the priority to record. Any change requires a reason."
        );
    }

    #[test]
    fn every_priority_is_offered_and_the_current_one_is_a_keep() {
        let options = priority_options("medium");
        assert_eq!(options.len(), 4);
        assert_eq!(options[1].label, "Keep — medium");
        assert_eq!(options[2].label, "Change to — high");
        assert!(options.iter().all(|option| option.id == option.value));
    }

    #[test]
    fn a_proposal_with_an_unknown_priority_is_refused() {
        let result = Proposal::from_args(&args(json!({
            "ticket_id": "TKT-1", "current_priority": "critical", "requested_priority": "high", "summary": "s"
        })));
        assert!(result.is_err());
    }

    #[test]
    fn the_result_never_claims_success_when_nothing_was_committed() {
        let refused = approval_result(
            "TKT-1",
            "r",
            &ExecuteOutcome { committed: false, result: json!({"refused": "already high"}) },
        );
        assert_eq!(refused["committed"], false);
        assert!(refused["next_step"].as_str().unwrap().contains("NOTHING was applied"));
    }

    #[test]
    fn the_actor_never_goes_back_to_the_model() {
        let applied = approval_result(
            "TKT-1",
            "r",
            &ExecuteOutcome {
                committed: true,
                result: json!({"actor_id": "8f14e45f-subject", "new_priority": "high"}),
            },
        );
        assert!(!applied.to_string().contains("8f14e45f"));
        assert_eq!(applied["result"]["new_priority"], "high");
    }
}
