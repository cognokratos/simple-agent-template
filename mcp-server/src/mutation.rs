//! Applying an approved mutation: one transaction, or nothing.
//!
//! `approval` proves a token is authentic and bound to this action, resource and
//! request. Everything that depends on *state* is here, and all of it happens
//! inside a single database transaction:
//!
//! 1. consume the nonce, which is what makes an approval single-use;
//! 2. lock the resource row and re-derive the authoritative state, because the
//!    world may have moved since the human was asked;
//! 3. re-validate the state transition against backend policy, because the
//!    model's advice is advice and the human's choice is a request;
//! 4. apply the mutation;
//! 5. append the audit record.
//!
//! Any failure rolls the whole thing back, including the nonce. That ordering
//! matters in both directions: consuming the nonce first means two concurrent
//! spends of one approval cannot both proceed, and rolling it back on failure
//! means a refused approval is not silently burned — the human's decision is
//! either applied and recorded, or nothing happened at all.
//!
//! What an application supplies
//! ---------------------------
//! An `Action` describes one mutation: its name, whether it carries a choice,
//! how to read the current authoritative choice for a resource, which
//! transitions are permitted, and how to apply it. The template ships one as a
//! demonstration (`set_ticket_priority`); the registry is the extension point.

use chrono::Utc;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sqlx::{PgPool, Postgres, Transaction};

use crate::approval::{ApprovalClaims, ApprovalVerifier};

/// What the caller asked us to do.
#[derive(Debug, Deserialize)]
pub struct ExecuteRequest {
    pub approval_token: String,
    /// The authenticated request the approval belongs to. Supplied by the agent
    /// from the gateway-injected `x-request-id`, and checked against the token,
    /// so an approval minted for one request cannot be spent on another.
    pub request_id: String,
}

/// What happened. `ok: false` with a reason is a normal outcome, not an error:
/// a legitimately approved change can still be refused by backend policy, and
/// the caller must be able to tell the user that nothing was applied.
#[derive(Debug, Serialize)]
pub struct ExecuteResponse {
    pub ok: bool,
    pub result: Value,
}

impl ExecuteResponse {
    fn refused(reason: impl Into<String>) -> Self {
        Self { ok: false, result: json!({ "refused": reason.into() }) }
    }

    fn applied(result: Value) -> Self {
        Self { ok: true, result }
    }
}

/// One application-defined mutation.
pub struct Action {
    /// Matched against the token's `action` claim.
    pub name: &'static str,
    /// Whether this action carries a choice the human selects.
    pub carries_choice: bool,
    /// Choices the application accepts at all. A choice outside this set is
    /// refused even with a valid signature: the gateway bounds shape, the agent
    /// checks the choice against the options it offered, and this is the third
    /// and authoritative check.
    pub allowed_choices: &'static [&'static str],
}

/// Registry of actions this server will apply.
///
/// Deliberately a fixed list rather than anything dynamic: the set of things a
/// human can authorize is a security property of the deployment, not
/// configuration.
pub const ACTIONS: &[Action] = &[Action {
    name: "set_ticket_priority",
    carries_choice: true,
    allowed_choices: &["low", "medium", "high", "urgent"],
}];

pub fn find_action(name: &str) -> Option<&'static Action> {
    ACTIONS.iter().find(|action| action.name == name)
}

/// Whether the approval feature is enabled for this deployment.
///
/// Absent secret means absent feature: the endpoint is not routed at all, so a
/// read-only deployment has no mutation surface to attack rather than a disabled
/// one. See `main`.
pub fn approvals_enabled(secret: Option<&Vec<u8>>) -> bool {
    secret.is_some_and(|value| !value.is_empty())
}

/// Apply the mutation a verified token authorizes.
pub async fn execute(
    pool: &PgPool,
    verifier: &ApprovalVerifier,
    request: &ExecuteRequest,
) -> Result<ExecuteResponse, String> {
    // Decode before the transaction: an unauthentic token must not cost a
    // connection or a lock. Binding to the authoritative choice needs the
    // current state, so the full `verify` happens inside.
    let peek = verifier.decode(&request.approval_token)?;
    let action = find_action(&peek.action)
        .ok_or_else(|| format!("unknown approval action: {}", peek.action))?;

    let mut tx = pool.begin().await.map_err(|error| {
        tracing::error!(%error, "could not begin the approval transaction");
        "the approved change could not be applied".to_string()
    })?;

    // 1. Single-use. The primary key is the nonce, so a concurrent second spend
    //    of the same approval either blocks here and then conflicts, or
    //    conflicts immediately. Either way exactly one proceeds.
    let inserted = sqlx::query(
        "INSERT INTO approval_nonces (nonce, action, resource_id, actor_id, request_id)
         VALUES ($1, $2, $3, $4, $5)
         ON CONFLICT (nonce) DO NOTHING",
    )
    .bind(&peek.nonce)
    .bind(&peek.action)
    .bind(&peek.resource_id)
    .bind(&peek.actor_id)
    .bind(&peek.request_id)
    .execute(&mut *tx)
    .await
    .map_err(|error| {
        tracing::error!(%error, "could not record the approval nonce");
        "the approved change could not be applied".to_string()
    })?;
    if inserted.rows_affected() == 0 {
        return Ok(ExecuteResponse::refused(
            "this approval has already been used",
        ));
    }

    // 2. Lock the row and read the authoritative state the human was shown.
    let current: Option<(String,)> =
        sqlx::query_as("SELECT priority FROM tickets WHERE id = $1 FOR UPDATE")
            .bind(&peek.resource_id)
            .fetch_optional(&mut *tx)
            .await
            .map_err(|error| {
                tracing::error!(%error, "could not read the approval target");
                "the approved change could not be applied".to_string()
            })?;
    let Some((current_priority,)) = current else {
        // Rolls back, so the nonce is not burned on a target that never existed.
        return Ok(ExecuteResponse::refused(format!(
            "{} does not exist",
            peek.resource_id
        )));
    };

    // 3. Full binding, now that the authoritative choice is known. A token
    //    issued against a state that has since changed is void.
    let expected = action.carries_choice.then_some(current_priority.as_str());
    let claims = match verifier.verify(
        &request.approval_token,
        action.name,
        &peek.resource_id,
        &request.request_id,
        expected,
    ) {
        Ok(claims) => claims,
        Err(error) => return Err(error),
    };

    match apply_policy(action, &claims, &current_priority) {
        Err(reason) => Ok(ExecuteResponse::refused(reason)),
        Ok(()) => {
            let outcome = apply(&mut tx, action, &claims, &current_priority).await?;
            tx.commit().await.map_err(|error| {
                tracing::error!(%error, "could not commit the approved change");
                "the approved change could not be applied".to_string()
            })?;
            Ok(ExecuteResponse::applied(outcome))
        }
    }
}

/// Backend policy, re-checked after the human approved.
///
/// The model's recommendation is advice and the human's selection is a request;
/// this is where either becomes permitted or refused. Running it *after* the
/// approval is deliberate: a refusal here is the control working, and the caller
/// is told plainly that nothing was applied.
fn apply_policy(
    action: &Action,
    claims: &ApprovalClaims,
    current_priority: &str,
) -> Result<(), String> {
    let Some(choice) = claims.choice.as_deref() else {
        if action.carries_choice {
            return Err("this action requires a choice and the approval carries none".into());
        }
        return Ok(());
    };

    if !action.allowed_choices.contains(&choice) {
        return Err(format!("{choice:?} is not a choice this action accepts"));
    }

    // An explicit state-transition check, rather than inferring legality from
    // the fact that a write would succeed.
    if choice == current_priority {
        return Err(format!("the ticket is already {current_priority} priority"));
    }

    // An override needs a stated reason. The claim's own `override_requested`
    // flag is not trusted for this: it is re-derived from the choice and the
    // authoritative state, so a minter that forgot to set it cannot skip the
    // requirement.
    let is_override = claims.expected_choice.as_deref() != Some(choice);
    if is_override && claims.effective_rationale().is_none() {
        return Err("overriding the current state requires a rationale".into());
    }
    Ok(())
}

/// The mutation and its audit record, both inside the caller's transaction.
async fn apply(
    tx: &mut Transaction<'_, Postgres>,
    action: &Action,
    claims: &ApprovalClaims,
    previous_priority: &str,
) -> Result<Value, String> {
    let choice = claims.choice.as_deref().unwrap_or_default();

    let updated = sqlx::query("UPDATE tickets SET priority = $1, updated_at = now() WHERE id = $2")
        .bind(choice)
        .bind(&claims.resource_id)
        .execute(&mut **tx)
        .await
        .map_err(|error| {
            tracing::error!(%error, "the approved mutation failed");
            "the approved change could not be applied".to_string()
        })?;
    if updated.rows_affected() != 1 {
        // Cannot happen behind the FOR UPDATE lock, but a silent zero-row update
        // would mean reporting a change that did not happen.
        return Err("the approved change affected no rows".to_string());
    }

    // Versioned policy context, so an old record stays interpretable after the
    // rules change. It records what was in force when the decision was taken,
    // not what is in force when the record is read.
    let policy_context = json!({
        "policy_version": POLICY_VERSION,
        "allowed_choices": action.allowed_choices,
        "authoritative_choice_at_approval": claims.expected_choice,
        "override": claims.expected_choice.as_deref() != Some(choice),
    });

    sqlx::query(
        "INSERT INTO ticket_audit (
             ticket_id, action, previous_priority, new_priority, actor_id, request_id,
             nonce, rationale, payload, policy_context, recorded_at
         ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
    )
    .bind(&claims.resource_id)
    .bind(&claims.action)
    .bind(previous_priority)
    .bind(choice)
    .bind(&claims.actor_id)
    .bind(&claims.request_id)
    .bind(&claims.nonce)
    .bind(claims.effective_rationale())
    .bind(&claims.payload)
    .bind(&policy_context)
    .bind(Utc::now())
    .execute(&mut **tx)
    .await
    .map_err(|error| {
        tracing::error!(%error, "could not append the audit record");
        "the approved change could not be applied".to_string()
    })?;

    Ok(json!({
        "resource_id": claims.resource_id,
        "action": claims.action,
        "previous_priority": previous_priority,
        "new_priority": choice,
        "actor_id": claims.actor_id,
        "override": policy_context["override"],
        "policy_version": POLICY_VERSION,
        // Echoed from the *signed* payload, so the caller can confirm what was
        // persisted rather than repeat what it believes it sent.
        "note_recorded": claims.payload_str("note").is_some(),
        "rationale_recorded": claims.effective_rationale().is_some(),
    }))
}

/// Bumped whenever the transition rules change, so audit records remain
/// interpretable against the policy that produced them.
pub const POLICY_VERSION: &str = "tickets-priority-policy/1";

#[cfg(test)]
mod tests {
    use super::*;
    use crate::approval::testing;

    #[test]
    fn the_action_registry_is_closed_and_addressable_by_name() {
        assert!(find_action("set_ticket_priority").is_some());
        for unknown in ["delete_ticket", "", "SET_TICKET_PRIORITY", "set_ticket_priority "] {
            assert!(find_action(unknown).is_none(), "{unknown:?}");
        }
    }

    #[test]
    fn approvals_are_disabled_without_a_secret() {
        assert!(!approvals_enabled(None));
        assert!(!approvals_enabled(Some(&Vec::new())));
        assert!(approvals_enabled(Some(&testing::TEST_SECRET.to_vec())));
    }

    fn action() -> &'static Action {
        find_action("set_ticket_priority").expect("registered")
    }

    #[test]
    fn a_legitimate_transition_with_a_rationale_is_permitted() {
        let claims = testing::claims();
        assert_eq!(apply_policy(action(), &claims, "medium"), Ok(()));
    }

    #[test]
    fn a_choice_outside_the_allowed_set_is_refused() {
        let mut claims = testing::claims();
        claims.choice = Some("critical".into());
        let error = apply_policy(action(), &claims, "medium").expect_err("must refuse");
        assert!(error.contains("not a choice this action accepts"), "{error}");
    }

    /// An explicit transition check, rather than inferring legality from a write
    /// that would happen to succeed.
    #[test]
    fn a_no_op_transition_is_refused() {
        let mut claims = testing::claims();
        claims.choice = Some("medium".into());
        claims.expected_choice = Some("medium".into());
        let error = apply_policy(action(), &claims, "medium").expect_err("must refuse");
        assert!(error.contains("already medium"), "{error}");
    }

    /// The override requirement is re-derived, never taken from the claim.
    #[test]
    fn an_override_without_a_rationale_is_refused_even_if_the_flag_says_otherwise() {
        let mut claims = testing::claims();
        claims.rationale = None;
        claims.override_requested = false; // a minter that "forgot"
        let error = apply_policy(action(), &claims, "medium").expect_err("must refuse");
        assert!(error.contains("requires a rationale"), "{error}");
    }

    #[test]
    fn a_blank_rationale_does_not_satisfy_the_override_requirement() {
        let mut claims = testing::claims();
        claims.rationale = Some("   \n ".into());
        let error = apply_policy(action(), &claims, "medium").expect_err("must refuse");
        assert!(error.contains("requires a rationale"), "{error}");
    }

    /// Confirming the authoritative state is not an override, so it needs no
    /// rationale.
    #[test]
    fn confirming_the_authoritative_choice_needs_no_rationale() {
        let mut claims = testing::claims();
        claims.choice = Some("high".into());
        claims.expected_choice = Some("high".into());
        claims.rationale = None;
        // Current state differs from the choice, so the transition itself is
        // legal; the choice equals what the authority proposed, so it is not an
        // override.
        assert_eq!(apply_policy(action(), &claims, "medium"), Ok(()));
    }

    #[test]
    fn a_choice_bearing_action_refuses_an_approval_with_no_choice() {
        let mut claims = testing::claims();
        claims.choice = None;
        let error = apply_policy(action(), &claims, "medium").expect_err("must refuse");
        assert!(error.contains("requires a choice"), "{error}");
    }
}
