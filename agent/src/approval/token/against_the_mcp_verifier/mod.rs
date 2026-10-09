//! Tokens minted by this agent, checked by the MCP server's verifier — its
//! actual source file (`mcp-server/src/approval.rs`), compiled into this test,
//! not a copy of its logic. If the two implementations ever disagree about a
//! single byte of the canonical encoding, these tests fail.

use std::sync::Arc;

use super::{tests::change, *};
use base64::engine::general_purpose::URL_SAFE_NO_PAD;

#[path = "../../../../../mcp-server/src/approval.rs"]
#[allow(dead_code, clippy::all)]
// Another crate's source: never reformatted from here.
#[rustfmt::skip]
mod mcp;

const SECRET: &[u8] = b"cross-check-secret-of-at-least-24";

fn verifier() -> mcp::ApprovalVerifier {
    mcp::ApprovalVerifier::new(Arc::new(SECRET.to_vec()))
}

fn mint(change: &ApprovedChange) -> String {
    Claims::for_change(change, 600, None, None).unwrap().mint(SECRET)
}

fn verify(token: &str) -> Result<mcp::ApprovalClaims, String> {
    verifier().verify(token, "set_ticket_priority", "TKT-1001", "11111111-1111-4111-8111-111111111111", Some("medium"))
}

#[test]
fn an_agent_minted_token_is_accepted_with_every_binding() {
    let claims = verify(&mint(&change())).expect("the MCP verifier accepts it");
    assert_eq!(claims.actor_id, "support-rep-1");
    assert_eq!(claims.choice.as_deref(), Some("high"));
    assert_eq!(claims.expected_choice.as_deref(), Some("medium"));
    assert_eq!(claims.effective_rationale(), Some("Customer has followed up twice."));
    assert_eq!(claims.payload_str("note"), Some("Escalated after the second follow-up."));
}

#[test]
fn a_non_ascii_payload_hashes_identically_on_both_sides() {
    let mut change = change();
    change.note = "Zoë’s 2nd follow-up — 日本語 \"quoted\"\nnew line".into();
    change.rationale = Some("Kundin hat zweimal nachgefragt: «dringend».".into());
    assert!(verify(&mint(&change)).is_ok());
}

#[test]
fn a_tampered_rationale_or_choice_is_refused() {
    let token = mint(&change());
    let (payload_b64, signature) = token.split_once('.').unwrap();
    let mut claims: serde_json::Value = serde_json::from_slice(&URL_SAFE_NO_PAD.decode(payload_b64).unwrap()).unwrap();
    for (field, forged) in [("rationale", "approved by the CEO"), ("choice", "urgent"), ("actor_id", "admin")] {
        let mut altered = claims.clone();
        altered[field] = serde_json::Value::String(forged.into());
        let forged_token = format!("{}.{signature}", URL_SAFE_NO_PAD.encode(canonical_json(&altered)));
        assert_eq!(verify(&forged_token).unwrap_err(), "approval token signature is invalid", "{field}");
    }
    claims["exp"] = serde_json::json!(i64::MAX);
    let forged_token = format!("{}.{signature}", URL_SAFE_NO_PAD.encode(canonical_json(&claims)));
    assert!(verify(&forged_token).is_err());
}

#[test]
fn a_token_from_another_secret_is_refused() {
    let token = Claims::for_change(&change(), 600, None, None).unwrap().mint(b"some-other-secret-of-24-chars!!");
    assert_eq!(verify(&token).unwrap_err(), "approval token signature is invalid");
}

#[test]
fn an_expired_token_is_refused() {
    let token = Claims::for_change(&change(), 600, Some(now_seconds() - 3_600), None).unwrap().mint(SECRET);
    assert_eq!(verify(&token).unwrap_err(), "approval token has expired");
}

#[test]
fn a_token_is_bound_to_its_request_and_the_state_the_human_saw() {
    let token = mint(&change());
    // Spent on another authenticated request.
    assert!(
        verifier()
            .verify(&token, "set_ticket_priority", "TKT-1001", "99999999-9999-4999-8999-999999999999", Some("medium"))
            .is_err()
    );
    // The ticket moved since the human was asked.
    assert!(
        verifier()
            .verify(&token, "set_ticket_priority", "TKT-1001", "11111111-1111-4111-8111-111111111111", Some("high"))
            .is_err()
    );
    // Another resource.
    assert!(
        verifier()
            .verify(&token, "set_ticket_priority", "TKT-1002", "11111111-1111-4111-8111-111111111111", Some("medium"))
            .is_err()
    );
}
