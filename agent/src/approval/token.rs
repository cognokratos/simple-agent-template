//! Approval tokens: the evidence the MCP server accepts as a human decision.
//!
//! Byte-for-byte the format `main`'s `approval.py` mints and
//! `mcp-server/src/approval.rs` verifies: HMAC-SHA256 over the base64url
//! encoding of the canonical JSON claim set, `"<payload_b64>.<signature_b64>"`.
//! `tests/approval_tokens.rs` compiles the MCP server's verifier source and
//! checks tokens minted here against it, so the two cannot drift silently.
//!
//! Minting requires an [`ApprovedChange`], which in turn can only be built from
//! a [`super::pending::VerifiedDecision`] — a value only the authenticated
//! interaction route can produce. The model's tool arguments supply the
//! proposal; they cannot supply the decision, the actor, or the request.

use std::time::{SystemTime, UNIX_EPOCH};

use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use hmac::{Hmac, Mac};
use serde::Serialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::config::{MAX_TOKEN_TTL_SECONDS, MIN_TOKEN_TTL_SECONDS};

type HmacSha256 = Hmac<Sha256>;

/// The claim set, exactly as `mcp-server/src/approval.rs::ApprovalClaims`
/// deserialises it.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct Claims {
    pub v: u8,
    pub exp: i64,
    pub action: String,
    pub resource_id: String,
    pub actor_id: String,
    pub request_id: String,
    pub choice: Option<String>,
    pub expected_choice: Option<String>,
    /// Recorded, never trusted: the MCP server re-derives it.
    pub override_requested: bool,
    pub rationale: Option<String>,
    pub payload: Value,
    pub payload_sha256: Option<String>,
    pub nonce: String,
}

/// A change a human approved through the authenticated interaction route.
/// Constructed only in `approval::gate`, from a verified decision.
#[derive(Debug, Clone, PartialEq)]
pub struct ApprovedChange {
    pub(super) action: &'static str,
    pub(super) resource_id: String,
    pub(super) actor_id: String,
    pub(super) request_id: String,
    pub(super) choice: String,
    pub(super) expected_choice: String,
    pub(super) rationale: Option<String>,
    pub(super) note: String,
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum MintError {
    #[error("token lifetime must be between {MIN_TOKEN_TTL_SECONDS} and {MAX_TOKEN_TTL_SECONDS} seconds")]
    Lifetime,
    #[error("approval requires an authenticated actor and a gateway request id")]
    MissingIdentity,
}

/// Serialise with sorted keys and no insignificant whitespace — the one form
/// the Python minter, this minter and the Rust verifier all produce
/// identically. Mirrors `canonical_json` in `mcp-server/src/approval.rs`.
pub fn canonical_json(value: &Value) -> String {
    match value {
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            let body: Vec<String> = keys
                .into_iter()
                .map(|key| format!("{}:{}", Value::String(key.clone()), canonical_json(&map[key])))
                .collect();
            format!("{{{}}}", body.join(","))
        }
        Value::Array(items) => format!("[{}]", items.iter().map(canonical_json).collect::<Vec<_>>().join(",")),
        other => other.to_string(),
    }
}

/// Hex SHA-256 of the canonical payload; `None` only for a null payload.
pub fn payload_hash(payload: &Value) -> Option<String> {
    if payload.is_null() {
        return None;
    }
    let digest = Sha256::digest(canonical_json(payload).as_bytes());
    Some(digest.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn now_seconds() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
}

impl Claims {
    /// Assemble the claim set for an approved change. Pure apart from the
    /// clock and the nonce, both of which can be supplied for tests.
    pub fn for_change(
        change: &ApprovedChange,
        ttl_seconds: u64,
        now: Option<i64>,
        nonce: Option<String>,
    ) -> Result<Self, MintError> {
        if !(MIN_TOKEN_TTL_SECONDS..=MAX_TOKEN_TTL_SECONDS).contains(&ttl_seconds) {
            return Err(MintError::Lifetime);
        }
        if change.actor_id.trim().is_empty() || change.request_id.trim().is_empty() {
            return Err(MintError::MissingIdentity);
        }
        let payload = if change.note.is_empty() { json!({}) } else { json!({ "note": change.note }) };
        Ok(Self {
            v: 1,
            exp: now.unwrap_or_else(now_seconds) + ttl_seconds as i64,
            action: change.action.to_string(),
            resource_id: change.resource_id.clone(),
            actor_id: change.actor_id.clone(),
            request_id: change.request_id.clone(),
            override_requested: change.choice != change.expected_choice,
            choice: Some(change.choice.clone()),
            expected_choice: Some(change.expected_choice.clone()),
            rationale: change.rationale.clone(),
            payload_sha256: payload_hash(&payload),
            payload,
            nonce: nonce.unwrap_or_else(|| uuid::Uuid::new_v4().to_string()),
        })
    }

    /// HMAC-SHA256 over the base64url payload, as the verifier expects.
    pub fn mint(&self, secret: &[u8]) -> String {
        let value = serde_json::to_value(self).expect("claims always serialise");
        let payload_b64 = URL_SAFE_NO_PAD.encode(canonical_json(&value).as_bytes());
        let mut mac = HmacSha256::new_from_slice(secret).expect("HMAC accepts any key length");
        mac.update(payload_b64.as_bytes());
        let signature = URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes());
        format!("{payload_b64}.{signature}")
    }
}

#[cfg(test)]
pub(super) mod tests {
    use super::*;

    pub(crate) fn change() -> ApprovedChange {
        ApprovedChange {
            action: "set_ticket_priority",
            resource_id: "TKT-1001".into(),
            actor_id: "support-rep-1".into(),
            request_id: "11111111-1111-4111-8111-111111111111".into(),
            choice: "high".into(),
            expected_choice: "medium".into(),
            rationale: Some("Customer has followed up twice.".into()),
            note: "Escalated after the second follow-up.".into(),
        }
    }

    #[test]
    fn canonical_json_matches_pythons_sorted_compact_form() {
        let value = json!({"b": 1, "a": {"y": [true, null], "x": "é\n\"q\""}});
        assert_eq!(canonical_json(&value), r#"{"a":{"x":"é\n\"q\"","y":[true,null]},"b":1}"#);
    }

    #[test]
    fn the_payload_digest_covers_the_note() {
        let claims = Claims::for_change(&change(), 600, Some(1_000), Some("n".into())).unwrap();
        assert_eq!(claims.payload, json!({"note": "Escalated after the second follow-up."}));
        assert_eq!(claims.payload_sha256, payload_hash(&claims.payload));
        assert!(claims.override_requested);
        assert_eq!(claims.exp, 1_600);
    }

    #[test]
    fn an_empty_note_signs_an_empty_payload() {
        let mut change = change();
        change.note.clear();
        let claims = Claims::for_change(&change, 600, Some(0), Some("n".into())).unwrap();
        assert_eq!(claims.payload, json!({}));
        // sha256("{}"), the same digest the Python minter produces.
        assert_eq!(
            claims.payload_sha256.as_deref(),
            Some("44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a")
        );
    }

    #[test]
    fn lifetimes_outside_the_ceiling_are_refused() {
        assert_eq!(Claims::for_change(&change(), 59, None, None), Err(MintError::Lifetime));
        assert_eq!(Claims::for_change(&change(), 1_801, None, None), Err(MintError::Lifetime));
    }

    #[test]
    fn a_change_without_identity_cannot_be_minted() {
        let mut change = change();
        change.actor_id = " ".into();
        assert_eq!(Claims::for_change(&change, 600, None, None), Err(MintError::MissingIdentity));
    }

    #[test]
    fn a_token_signed_with_another_secret_differs() {
        let claims = Claims::for_change(&change(), 600, Some(0), Some("n".into())).unwrap();
        assert_ne!(claims.mint(b"one-secret-of-at-least-24-chars"), claims.mint(b"two-secret-of-at-least-24-chars"));
    }
}

/// Tokens minted here, checked by the MCP server's own verifier source.
#[cfg(test)]
mod against_the_mcp_verifier;
