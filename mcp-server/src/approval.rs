//! Human approval tokens: the only authority under which this service mutates
//! state.
//!
//! The token *is* the payload. Every mutation parameter is read from the signed
//! claims rather than from tool arguments, so a model cannot alter, drop, or
//! re-draft any part of what the human approved — it never gets to restate it.
//!
//! Verification is a pure function of a token and the facts the caller
//! recomputed, with no database or server state, so the whole matrix of
//! rejections is unit-testable. State-dependent checks — nonce consumption,
//! the resource's current state, the audit insert — live in `mutation`, inside
//! one transaction.
//!
//! What is generic and what is not
//! -------------------------------
//! `action`, `choice` and `payload` are application-defined. This module does
//! not know what actions exist or which choices are legitimate; it proves that
//! *some* human authorized *this* action on *this* resource for *this* request,
//! and that nothing has been altered since. The application supplies the action
//! registry and the choice authority (see `mutation`).

use std::sync::Arc;

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use chrono::Utc;
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

type HmacSha256 = Hmac<Sha256>;

/// The only claim-set version this server understands.
const SUPPORTED_VERSION: u8 = 1;

/// Longest lifetime this server will honour, whatever a token claims for itself.
///
/// The minter caps its own TTL, but the minter is not the trust boundary: a
/// misconfigured or compromised issuer could otherwise mint approvals that never
/// expire, and nothing downstream would notice. Keep in step with the
/// `token_ttl_seconds` ceiling in `agent/src/nat_streaming_react/approval.py`.
pub const MAX_APPROVAL_LIFETIME_SECONDS: i64 = 1_800;

/// Tolerance for clock drift between minter and verifier, applied only to the
/// lifetime ceiling. Expiry itself is enforced strictly — leniency there would
/// extend the window an approval stays spendable.
const CLOCK_SKEW_TOLERANCE_SECONDS: i64 = 60;

/// Minimum shared-secret length. Enforced at startup, not here, so a
/// misconfiguration fails before the first request rather than on it.
pub const MIN_SECRET_LENGTH: usize = 24;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ApprovalClaims {
    pub v: u8,
    pub exp: i64,
    /// Application-defined action name. Bound by `verify`, dispatched by
    /// `mutation::apply`.
    pub action: String,
    /// The resource this approval is for. Opaque to this module.
    pub resource_id: String,
    /// The authenticated end user, taken from the gateway-injected identity
    /// header and never from the model. This is what the audit trail names.
    pub actor_id: String,
    /// Correlates the approval to the one authenticated request it belongs to.
    pub request_id: String,
    /// The choice the human selected, for a choice-bearing action.
    #[serde(default)]
    pub choice: Option<String>,
    /// The choice the authoritative backend computed at the moment the human was
    /// asked. Re-derived at execution time; a mismatch voids the token, because
    /// the world the human was shown no longer holds.
    #[serde(default)]
    pub expected_choice: Option<String>,
    /// True when the human chose something other than `expected_choice`.
    /// Recorded, never trusted: `mutation` re-derives it.
    #[serde(default)]
    pub override_requested: bool,
    /// The human's stated reason, required for an override.
    #[serde(default)]
    pub rationale: Option<String>,
    /// Application-owned fields, carried inside the signature so the mutation
    /// never depends on the model resending identical text.
    #[serde(default)]
    pub payload: Value,
    /// Redundant with `payload`, which is itself covered by the signature. This
    /// is a *minting* integrity assertion, not a tampering defence: an attacker
    /// cannot make the two disagree without the secret, but a minter that hashes
    /// one payload and ships another is caught here rather than persisting the
    /// wrong values under a valid signature.
    #[serde(default)]
    pub payload_sha256: Option<String>,
    pub nonce: String,
}

impl ApprovalClaims {
    /// The human's rationale, or nothing if absent or blank. Whitespace-only
    /// approval text is not a rationale.
    pub fn effective_rationale(&self) -> Option<&str> {
        self.rationale.as_deref().map(str::trim).filter(|text| !text.is_empty())
    }

    /// A string field from the application-owned payload, trimmed and non-empty.
    pub fn payload_str(&self, key: &str) -> Option<&str> {
        self.payload
            .get(key)
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
    }
}

/// Hex SHA-256 of the canonical form of an application payload.
///
/// Canonical because JSON object order is not significant: the minter and the
/// verifier must agree on the bytes being hashed, and they are different
/// languages. Sorted keys with no insignificant whitespace is the one form both
/// can produce identically.
pub fn payload_hash(payload: &Value) -> Option<String> {
    if payload.is_null() {
        return None;
    }
    let canonical = canonical_json(payload);
    let digest = Sha256::digest(canonical.as_bytes());
    Some(digest.iter().map(|byte| format!("{byte:02x}")).collect())
}

/// Serialize with object keys sorted and no insignificant whitespace.
fn canonical_json(value: &Value) -> String {
    match value {
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            let body: Vec<String> = keys
                .into_iter()
                .map(|key| {
                    format!(
                        "{}:{}",
                        Value::String(key.clone()),
                        canonical_json(&map[key])
                    )
                })
                .collect();
            format!("{{{}}}", body.join(","))
        }
        Value::Array(items) => {
            let body: Vec<String> = items.iter().map(canonical_json).collect();
            format!("[{}]", body.join(","))
        }
        other => other.to_string(),
    }
}

/// Verifies approval tokens against the shared HMAC secret.
#[derive(Clone)]
pub struct ApprovalVerifier {
    secret: Arc<Vec<u8>>,
}

impl ApprovalVerifier {
    pub fn new(secret: Arc<Vec<u8>>) -> Self {
        Self { secret }
    }

    /// Verify signature, version and lifetime, and return the claims.
    ///
    /// Binding is checked separately by [`Self::verify`]; this only proves the
    /// claims are authentic.
    pub fn decode(&self, token: &str) -> Result<ApprovalClaims, String> {
        let (payload_b64, signature_b64) = token
            .split_once('.')
            .ok_or_else(|| "approval token has invalid format".to_string())?;
        let signature = URL_SAFE_NO_PAD
            .decode(signature_b64)
            .map_err(|_| "approval token signature is not base64url".to_string())?;
        let mut mac = HmacSha256::new_from_slice(&self.secret)
            .map_err(|_| "approval token verifier is misconfigured".to_string())?;
        mac.update(payload_b64.as_bytes());
        // verify_slice is constant time and length-checked.
        mac.verify_slice(&signature)
            .map_err(|_| "approval token signature is invalid".to_string())?;

        let payload = URL_SAFE_NO_PAD
            .decode(payload_b64)
            .map_err(|_| "approval token payload is not base64url".to_string())?;
        let claims: ApprovalClaims = serde_json::from_slice(&payload)
            .map_err(|_| "approval token payload is invalid".to_string())?;

        if claims.v != SUPPORTED_VERSION {
            return Err("approval token version is not supported".to_string());
        }
        let now = Utc::now().timestamp();
        if claims.exp < now {
            return Err("approval token has expired".to_string());
        }
        if claims.exp - now > MAX_APPROVAL_LIFETIME_SECONDS + CLOCK_SKEW_TOLERANCE_SECONDS {
            return Err(
                "approval token lifetime exceeds the maximum this server accepts".to_string()
            );
        }
        Ok(claims)
    }

    /// Verify that a token authorizes this exact action on this exact resource,
    /// for this request, against the choice the authority just recomputed.
    ///
    /// `expected_choice` is `None` for actions that carry no choice, and the
    /// token must then not carry one either.
    pub fn verify(
        &self,
        token: &str,
        action: &str,
        resource_id: &str,
        request_id: &str,
        expected_choice: Option<&str>,
    ) -> Result<ApprovalClaims, String> {
        let claims = self.decode(token)?;
        if claims.action != action || claims.resource_id != resource_id {
            return Err("approval token is not bound to this action and resource".to_string());
        }
        if claims.request_id != request_id {
            return Err("approval token is not bound to this authenticated request".to_string());
        }
        if claims.payload_sha256 != payload_hash(&claims.payload) {
            return Err("approval token payload does not match its own signature".to_string());
        }
        // For choice-bearing actions the authoritative result must still be what
        // the human was shown: if the resource or the policy changed underneath
        // the approval, the token is void.
        if claims.expected_choice.as_deref() != expected_choice {
            return Err(if expected_choice.is_none() {
                "approval token must not carry a choice for this action".to_string()
            } else {
                "approval token was issued against a different authoritative choice".to_string()
            });
        }
        if claims.actor_id.trim().is_empty()
            || claims.request_id.trim().is_empty()
            || claims.nonce.trim().is_empty()
        {
            return Err(
                "approval token is missing audit identity or request correlation".to_string()
            );
        }
        Ok(claims)
    }
}

#[cfg(test)]
pub(crate) mod testing {
    //! Token minting, mirroring `agent/src/nat_streaming_react/approval.py`, so
    //! the verifier can be exercised against tokens produced the way the real
    //! minter produces them.

    use super::*;

    pub const TEST_SECRET: &[u8] = b"a-test-secret-of-at-least-24-characters";

    pub fn verifier() -> ApprovalVerifier {
        ApprovalVerifier::new(Arc::new(TEST_SECRET.to_vec()))
    }

    pub fn mint_with_secret(secret: &[u8], claims: &ApprovalClaims) -> String {
        // serde_json with sorted keys is what the Python minter produces via
        // json.dumps(sort_keys=True); the signature covers the encoded payload,
        // so both sides only have to agree on the bytes they sign.
        let payload = serde_json::to_value(claims).expect("claims serialize");
        let encoded = canonical_json(&payload);
        let payload_b64 = URL_SAFE_NO_PAD.encode(encoded.as_bytes());
        let mut mac = HmacSha256::new_from_slice(secret).expect("secret");
        mac.update(payload_b64.as_bytes());
        let signature = URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes());
        format!("{payload_b64}.{signature}")
    }

    pub fn mint(claims: &ApprovalClaims) -> String {
        mint_with_secret(TEST_SECRET, claims)
    }

    /// A valid choice-bearing approval, as the minter would produce it.
    pub fn claims() -> ApprovalClaims {
        let payload = serde_json::json!({"note": "Escalated after the customer's second follow-up."});
        ApprovalClaims {
            v: 1,
            exp: Utc::now().timestamp() + 600,
            action: "set_ticket_priority".to_string(),
            resource_id: "TKT-1001".to_string(),
            actor_id: "support-rep-1".to_string(),
            request_id: "11111111-1111-4111-8111-111111111111".to_string(),
            choice: Some("high".to_string()),
            expected_choice: Some("medium".to_string()),
            override_requested: true,
            rationale: Some("Customer has followed up twice with no resolution.".to_string()),
            payload_sha256: payload_hash(&payload),
            payload,
            nonce: "22222222-2222-4222-8222-222222222222".to_string(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::testing::*;
    use super::*;

    const ACTION: &str = "set_ticket_priority";
    const RESOURCE: &str = "TKT-1001";
    const REQUEST: &str = "11111111-1111-4111-8111-111111111111";
    const EXPECTED: Option<&str> = Some("medium");

    fn check(claims: &ApprovalClaims) -> Result<ApprovalClaims, String> {
        verifier().verify(&mint(claims), ACTION, RESOURCE, REQUEST, EXPECTED)
    }

    #[test]
    fn a_well_formed_approval_verifies_and_returns_its_claims() {
        let verified = check(&claims()).expect("must verify");
        assert_eq!(verified.actor_id, "support-rep-1");
        assert_eq!(verified.choice.as_deref(), Some("high"));
        assert_eq!(
            verified.payload_str("note"),
            Some("Escalated after the customer's second follow-up.")
        );
        assert_eq!(
            verified.effective_rationale(),
            Some("Customer has followed up twice with no resolution.")
        );
    }

    /// The whole boundary rests on this one: without the secret, nothing can be
    /// authorized.
    #[test]
    fn a_token_signed_with_the_wrong_secret_is_refused() {
        let forged = mint_with_secret(b"a-different-secret-also-24-chars+", &claims());
        let error = verifier()
            .verify(&forged, ACTION, RESOURCE, REQUEST, EXPECTED)
            .expect_err("a forged signature must not verify");
        assert!(error.contains("signature is invalid"), "{error}");
    }

    #[test]
    fn a_token_with_a_tampered_payload_is_refused() {
        let token = mint(&claims());
        let (payload_b64, signature) = token.split_once('.').expect("format");
        let mut payload: Value = serde_json::from_slice(
            &URL_SAFE_NO_PAD.decode(payload_b64).expect("base64"),
        )
        .expect("json");
        // Escalate the approved change: same signature, different resource.
        payload["resource_id"] = Value::String("TKT-1002".into());
        let tampered = format!(
            "{}.{signature}",
            URL_SAFE_NO_PAD.encode(canonical_json(&payload).as_bytes())
        );
        let error = verifier()
            .verify(&tampered, ACTION, RESOURCE, REQUEST, EXPECTED)
            .expect_err("a tampered payload must not verify");
        assert!(error.contains("signature is invalid"), "{error}");
    }

    #[test]
    fn a_malformed_token_is_refused_rather_than_panicking() {
        for token in ["", ".", "not-a-token", "a.b", "$$$.$$$", "...."] {
            assert!(
                verifier().verify(token, ACTION, RESOURCE, REQUEST, EXPECTED).is_err(),
                "{token:?}"
            );
        }
    }

    #[test]
    fn an_expired_token_is_refused() {
        let mut expired = claims();
        expired.exp = Utc::now().timestamp() - 1;
        let error = check(&expired).expect_err("an expired approval must not verify");
        assert!(error.contains("expired"), "{error}");
    }

    /// The minter caps its own TTL, but the minter is not the trust boundary.
    #[test]
    fn a_token_claiming_an_excessive_lifetime_is_refused() {
        let mut forever = claims();
        forever.exp = Utc::now().timestamp() + MAX_APPROVAL_LIFETIME_SECONDS * 10;
        let error = check(&forever).expect_err("an unbounded lifetime must not verify");
        assert!(error.contains("lifetime exceeds"), "{error}");
    }

    #[test]
    fn the_lifetime_ceiling_tolerates_clock_skew_but_not_more() {
        let mut edge = claims();
        edge.exp = Utc::now().timestamp() + MAX_APPROVAL_LIFETIME_SECONDS + 30;
        assert!(check(&edge).is_ok(), "a minute of skew must be tolerated");

        edge.exp = Utc::now().timestamp() + MAX_APPROVAL_LIFETIME_SECONDS + 600;
        assert!(check(&edge).is_err(), "ten minutes is not clock skew");
    }

    #[test]
    fn an_unsupported_version_is_refused() {
        let mut future = claims();
        future.v = 2;
        let error = check(&future).expect_err("an unknown claim version must not verify");
        assert!(error.contains("version"), "{error}");
    }

    /// Binding. A valid approval for one thing must not authorize another.
    #[test]
    fn an_approval_is_bound_to_its_action_resource_and_request() {
        let valid = claims();
        let token = mint(&valid);
        let verifier = verifier();

        let wrong_action = verifier
            .verify(&token, "delete_ticket", RESOURCE, REQUEST, EXPECTED)
            .expect_err("wrong action");
        assert!(wrong_action.contains("not bound to this action"), "{wrong_action}");

        let wrong_resource = verifier
            .verify(&token, ACTION, "TKT-1002", REQUEST, EXPECTED)
            .expect_err("wrong resource");
        assert!(wrong_resource.contains("not bound to this action"), "{wrong_resource}");

        let wrong_request = verifier
            .verify(&token, ACTION, RESOURCE, "another-request-id", EXPECTED)
            .expect_err("wrong request");
        assert!(
            wrong_request.contains("not bound to this authenticated request"),
            "{wrong_request}"
        );
    }

    /// The world the human was shown must still hold. If the authoritative
    /// choice moved under the approval, the token is void rather than applied
    /// against a state nobody agreed to.
    #[test]
    fn an_approval_is_void_when_the_authoritative_choice_has_moved() {
        let token = mint(&claims());
        let error = verifier()
            .verify(&token, ACTION, RESOURCE, REQUEST, Some("high"))
            .expect_err("a moved authoritative choice must void the approval");
        assert!(error.contains("different authoritative choice"), "{error}");
    }

    #[test]
    fn a_choice_free_action_must_not_carry_a_choice() {
        let token = mint(&claims());
        let error = verifier()
            .verify(&token, ACTION, RESOURCE, REQUEST, None)
            .expect_err("a choice-free action must not accept a choice-bearing token");
        assert!(error.contains("must not carry a choice"), "{error}");

        let mut choice_free = claims();
        choice_free.choice = None;
        choice_free.expected_choice = None;
        choice_free.override_requested = false;
        let verified = verifier()
            .verify(&mint(&choice_free), ACTION, RESOURCE, REQUEST, None)
            .expect("a choice-free approval must verify");
        assert!(verified.choice.is_none());
    }

    /// A minter that hashes one payload and ships another is caught before the
    /// wrong values are persisted under a valid signature.
    #[test]
    fn a_payload_digest_that_disagrees_with_the_payload_is_refused() {
        let mut inconsistent = claims();
        inconsistent.payload_sha256 = payload_hash(&serde_json::json!({"note": "something else"}));
        let error = check(&inconsistent).expect_err("an inconsistent digest must not verify");
        assert!(error.contains("does not match its own signature"), "{error}");
    }

    #[test]
    fn the_payload_digest_is_insensitive_to_key_order() {
        let a = serde_json::json!({"note": "n", "assignee": "support-rep-2"});
        let b = serde_json::json!({"assignee": "support-rep-2", "note": "n"});
        assert_eq!(payload_hash(&a), payload_hash(&b));
        assert_ne!(payload_hash(&a), payload_hash(&serde_json::json!({"note": "n"})));
        assert_eq!(payload_hash(&Value::Null), None);
    }

    /// An approval with nobody's name on it cannot be audited, so it cannot be
    /// spent.
    #[test]
    fn an_approval_without_an_identity_or_a_nonce_is_refused() {
        for mutate in [
            (|c: &mut ApprovalClaims| c.actor_id = "   ".into()) as fn(&mut ApprovalClaims),
            |c: &mut ApprovalClaims| c.nonce = String::new(),
        ] {
            let mut anonymous = claims();
            mutate(&mut anonymous);
            let error = check(&anonymous).expect_err("must not verify");
            assert!(
                error.contains("missing audit identity")
                    || error.contains("not bound to this authenticated request"),
                "{error}"
            );
        }
    }

    /// Cross-language: a token minted by the Python agent is accepted here.
    ///
    /// Deliberately checks the signature and the payload digest directly rather
    /// than calling `decode`, because `decode` enforces expiry: a pinned token
    /// fixture would pass today and fail whenever its `exp` passed, turning a
    /// cross-language contract test into a time bomb. Expiry is covered by its
    /// own tests against freshly minted claims.
    ///
    /// The signature covers the received base64 payload verbatim, so JSON
    /// encoding differences between the two languages cannot break verification
    /// itself. `payload_sha256` is the one field both sides compute
    /// independently, so this is what proves the two canonical encoders agree.
    #[test]
    fn a_token_minted_by_the_python_agent_is_accepted() {
        // Produced by mint_token() in agent/src/nat_streaming_react/approval.py
        // with TEST_SECRET. Regenerate only if the claim format changes.
        const PYTHON_TOKEN: &str = "eyJhY3Rpb24iOiJzZXRfdGlja2V0X3ByaW9yaXR5IiwiYWN0b3JfaWQiOiJzdXBwb3J0LXJlcC0xIiwiY2hvaWNlIjoiaGlnaCIsImV4cCI6MTc4OTg5OTI4OCwiZXhwZWN0ZWRfY2hvaWNlIjoibWVkaXVtIiwibm9uY2UiOiJkYjVkM2U5OS01NDdlLTQ1OWQtYTIwZi1hOTVkNGRhOThmZDQiLCJvdmVycmlkZV9yZXF1ZXN0ZWQiOnRydWUsInBheWxvYWQiOnsibm90ZSI6IkRvY3VtZW50ZWQgYWZ0ZXIgdGhlIGN1c3RvbWVyJ3Mgc2Vjb25kIGZvbGxvdy11cC4gVW5pY29kZTogWm_DqyDinJMifSwicGF5bG9hZF9zaGEyNTYiOiI1MWFjYmIxMGNmY2YyZWE4MDBkMTI3NjI3YWZlY2FmZGJiNjVkN2IxNDJjOGQ3ZjdhMmJkMWY0NWNiOTFmMGEwIiwicmF0aW9uYWxlIjoiQ3VzdG9tZXIgaGFzIGZvbGxvd2VkIHVwIHR3aWNlIHdpdGggbm8gcmVzb2x1dGlvbi4iLCJyZXF1ZXN0X2lkIjoiMTExMTExMTEtMTExMS00MTExLTgxMTEtMTExMTExMTExMTExIiwicmVzb3VyY2VfaWQiOiJUS1QtMTAwMSIsInYiOjF9.X21SyZoYnU8OU8fYk23shsM6JugTdWQBhUuyWsNeWtc";

        let (payload_b64, signature_b64) = PYTHON_TOKEN.split_once('.').expect("format");
        let mut mac = HmacSha256::new_from_slice(TEST_SECRET).expect("secret");
        mac.update(payload_b64.as_bytes());
        mac.verify_slice(&URL_SAFE_NO_PAD.decode(signature_b64).expect("base64"))
            .expect("a token minted by the agent must verify in the MCP server");

        let claims: ApprovalClaims = serde_json::from_slice(
            &URL_SAFE_NO_PAD.decode(payload_b64).expect("base64"),
        )
        .expect("the agent's claim set must deserialize into this server's type");

        assert_eq!(claims.v, SUPPORTED_VERSION);
        assert_eq!(claims.action, "set_ticket_priority");
        assert_eq!(claims.resource_id, "TKT-1001");
        assert_eq!(claims.actor_id, "support-rep-1");
        assert_eq!(claims.choice.as_deref(), Some("high"));
        assert_eq!(claims.expected_choice.as_deref(), Some("medium"));
        assert!(claims.override_requested);
        assert!(!claims.nonce.is_empty());
        // The digest the Python side computed must equal the one this side does.
        assert_eq!(
            claims.payload_sha256,
            payload_hash(&claims.payload),
            "the two canonical JSON encoders no longer agree"
        );
        assert!(
            claims.payload_str("note").expect("note").contains("Zoë"),
            "non-ASCII payload text must survive both encoders"
        );
    }

    #[test]
    fn blank_optional_text_is_reported_as_absent_rather_than_empty() {
        let mut blank = claims();
        blank.rationale = Some("   ".into());
        blank.payload = serde_json::json!({"note": "  "});
        blank.payload_sha256 = payload_hash(&blank.payload);
        let verified = check(&blank).expect("must verify");
        assert_eq!(verified.effective_rationale(), None);
        assert_eq!(verified.payload_str("note"), None);
    }
}
