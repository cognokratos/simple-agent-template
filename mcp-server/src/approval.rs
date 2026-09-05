//! Human approval tokens: the only authority under which this service mutates
//! state.
//!
//! The token *is* the payload. Every mutation parameter is read from the signed
//! claims rather than from tool arguments, so a model cannot alter, drop, or
//! re-draft any part of what the human approved — it never gets to restate it.
//!
//! Verification is a pure function of a token and the facts the caller
//! recomputed, with no database or server state, so the whole matrix of
//! rejections is unit-testable.

use std::sync::Arc;

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use chrono::Utc;
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
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

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ApprovalClaims {
    pub v: u8,
    pub exp: i64,
    /// One of `commit`, `shortlist`, `assign`.
    pub action: String,
    pub etf_id: String,
    pub actor_id: String,
    pub request_id: String,
    pub rules_decision: Option<String>,
    pub llm_recommendation: Option<String>,
    pub requested_decision: Option<String>,
    pub override_requested: bool,
    pub assignee: Option<String>,
    /// Redundant with `research_note`, which is itself covered by the signature.
    /// This is a *minting* integrity assertion, not a tampering defence: an
    /// attacker cannot make the two disagree without the secret, but a minter that
    /// hashes one text and ships another is caught here rather than persisting the
    /// wrong note under a valid signature.
    pub research_note_sha256: Option<String>,
    /// Exact research note the human approved. Carried inside the signed token so
    /// the mutation never depends on the model resending the identical text.
    #[serde(default)]
    pub research_note: Option<String>,
    pub override_rationale: Option<String>,
    pub nonce: String,
}

impl ApprovalClaims {
    /// The note to persist: the approved text, or nothing if it is absent or
    /// blank. Whitespace-only approval text is not a note.
    pub fn effective_research_note(&self) -> Option<&str> {
        self.research_note.as_deref().map(str::trim).filter(|text| !text.is_empty())
    }

    /// The human's rationale for an override, or nothing if it is absent or blank.
    pub fn effective_override_rationale(&self) -> Option<&str> {
        self.override_rationale.as_deref().map(str::trim).filter(|text| !text.is_empty())
    }

    /// The research owner to record, or nothing if absent or blank.
    pub fn effective_assignee(&self) -> Option<&str> {
        self.assignee.as_deref().map(str::trim).filter(|value| !value.is_empty())
    }
}

/// Hex SHA-256 of a research note, distinguishing absent text from empty text.
pub fn research_note_hash(note: Option<&str>) -> Option<String> {
    note.map(|text| {
        let digest = Sha256::digest(text.as_bytes());
        digest.iter().map(|byte| format!("{byte:02x}")).collect::<String>()
    })
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
    /// Payload binding is checked separately by [`Self::verify`]; this only proves
    /// the claims are authentic, so omitted mutation arguments can be adopted from
    /// them rather than requiring the model to replay the approved payload.
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

    /// Verify that a token authorizes this exact action on this exact ETF, for
    /// this request, against the deterministic decision the caller just
    /// recomputed.
    ///
    /// `rules_decision` is `None` for actions that carry no decision, and the
    /// token must then not carry one either.
    pub fn verify(
        &self,
        token: &str,
        action: &str,
        etf_id: &str,
        request_id: &str,
        rules_decision: Option<&str>,
    ) -> Result<ApprovalClaims, String> {
        let claims = self.decode(token)?;
        if claims.action != action || claims.etf_id != etf_id {
            return Err("approval token is not bound to this action and ETF".to_string());
        }
        if claims.request_id != request_id {
            return Err("approval token is not bound to this authenticated request".to_string());
        }
        if claims.research_note_sha256 != research_note_hash(claims.research_note.as_deref()) {
            return Err("approval token research note does not match its own signature".to_string());
        }
        // For decision-bearing actions the deterministic result must still be what
        // the human was shown: if the ETF record or the policy changed underneath
        // the approval, the token is void.
        if claims.rules_decision.as_deref() != rules_decision {
            return Err(if rules_decision.is_none() {
                "approval token must not carry a decision for this action".to_string()
            } else {
                "approval token was issued against a different deterministic decision".to_string()
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
    //! Token minting, mirroring `agent/src/nat_streaming_react/approval.py`, so the
    //! verifier can be exercised against tokens produced the way the real minter
    //! produces them.

    use super::*;

    pub const TEST_SECRET: &[u8] = b"a-test-secret-of-at-least-24-characters";

    pub fn verifier() -> ApprovalVerifier {
        ApprovalVerifier::new(Arc::new(TEST_SECRET.to_vec()))
    }

    pub fn mint(secret: &[u8], claims: &ApprovalClaims) -> String {
        let payload = serde_json::to_vec(claims).expect("serialise claims");
        let payload_b64 = URL_SAFE_NO_PAD.encode(payload);
        let mut mac = HmacSha256::new_from_slice(secret).expect("hmac accepts any key length");
        mac.update(payload_b64.as_bytes());
        let signature = URL_SAFE_NO_PAD.encode(mac.finalize().into_bytes());
        format!("{payload_b64}.{signature}")
    }

    /// A well-formed evaluation approval, which each test then mutates in one way.
    pub fn commit_claims() -> ApprovalClaims {
        ApprovalClaims {
            v: SUPPORTED_VERSION,
            exp: Utc::now().timestamp() + 300,
            action: "commit".to_string(),
            etf_id: "VWCE-XETRA".to_string(),
            actor_id: "researcher-1".to_string(),
            request_id: "req-1".to_string(),
            rules_decision: Some("research".to_string()),
            llm_recommendation: Some("research".to_string()),
            requested_decision: Some("research".to_string()),
            override_requested: false,
            assignee: None,
            research_note_sha256: None,
            research_note: None,
            override_rationale: None,
            nonce: "nonce-1".to_string(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::testing::*;
    use super::*;

    fn verify_commit(claims: &ApprovalClaims) -> Result<ApprovalClaims, String> {
        verifier().verify(
            &mint(TEST_SECRET, claims),
            "commit",
            "VWCE-XETRA",
            "req-1",
            Some("research"),
        )
    }

    #[test]
    fn a_well_formed_token_verifies() {
        assert!(verify_commit(&commit_claims()).is_ok());
    }

    #[test]
    fn a_token_signed_with_another_secret_is_rejected() {
        let token = mint(b"a-different-secret-of-24-plus-chars", &commit_claims());
        let error = verifier()
            .verify(&token, "commit", "VWCE-XETRA", "req-1", Some("research"))
            .expect_err("must reject");
        assert!(error.contains("signature"), "{error}");
    }

    #[test]
    fn a_tampered_payload_is_rejected() {
        let token = mint(TEST_SECRET, &commit_claims());
        let (payload, signature) = token.split_once('.').expect("token has two parts");
        let mut forged: ApprovalClaims =
            serde_json::from_slice(&URL_SAFE_NO_PAD.decode(payload).expect("decode"))
                .expect("parse");
        forged.requested_decision = Some("shortlist".to_string());
        let forged_payload =
            URL_SAFE_NO_PAD.encode(serde_json::to_vec(&forged).expect("serialise"));
        let token = format!("{forged_payload}.{signature}");

        assert!(verifier()
            .verify(&token, "commit", "VWCE-XETRA", "req-1", Some("research"))
            .is_err());
    }

    #[test]
    fn an_expired_token_is_rejected() {
        let claims = ApprovalClaims { exp: Utc::now().timestamp() - 1, ..commit_claims() };
        assert_eq!(
            verify_commit(&claims).expect_err("must reject"),
            "approval token has expired"
        );
    }

    /// The minter caps its own TTL; this server must not take that on trust.
    #[test]
    fn a_token_outliving_the_server_ceiling_is_rejected() {
        let claims = ApprovalClaims {
            exp: Utc::now().timestamp() + MAX_APPROVAL_LIFETIME_SECONDS * 100,
            ..commit_claims()
        };
        let error = verify_commit(&claims).expect_err("must reject");
        assert!(error.contains("lifetime exceeds"), "{error}");
    }

    #[test]
    fn a_token_at_the_ceiling_is_still_accepted() {
        let claims = ApprovalClaims {
            exp: Utc::now().timestamp() + MAX_APPROVAL_LIFETIME_SECONDS,
            ..commit_claims()
        };
        assert!(verify_commit(&claims).is_ok());
    }

    #[test]
    fn an_unsupported_version_is_rejected() {
        let claims = ApprovalClaims { v: SUPPORTED_VERSION + 1, ..commit_claims() };
        assert!(verify_commit(&claims).is_err());
    }

    #[test]
    fn a_token_for_another_action_or_etf_is_rejected() {
        let claims = ApprovalClaims { action: "shortlist".to_string(), ..commit_claims() };
        assert!(verify_commit(&claims).is_err());

        let claims = ApprovalClaims { etf_id: "IWDA-AMS".to_string(), ..commit_claims() };
        assert!(verify_commit(&claims).is_err());
    }

    #[test]
    fn a_token_for_another_request_is_rejected() {
        let claims = ApprovalClaims { request_id: "req-2".to_string(), ..commit_claims() };
        let error = verify_commit(&claims).expect_err("must reject");
        assert!(error.contains("authenticated request"), "{error}");
    }

    /// The ETF record or the policy changed underneath the approval: the human
    /// authorised a decision the engine no longer makes.
    #[test]
    fn a_token_issued_against_a_different_decision_is_rejected() {
        let claims =
            ApprovalClaims { rules_decision: Some("shortlist".to_string()), ..commit_claims() };
        let error = verify_commit(&claims).expect_err("must reject");
        assert!(error.contains("different deterministic decision"), "{error}");
    }

    #[test]
    fn an_assignment_token_must_not_carry_a_decision() {
        let claims = ApprovalClaims {
            action: "assign".to_string(),
            assignee: Some("victor".to_string()),
            ..commit_claims()
        };
        let error = verifier()
            .verify(&mint(TEST_SECRET, &claims), "assign", "VWCE-XETRA", "req-1", None)
            .expect_err("must reject");
        assert!(error.contains("must not carry a decision"), "{error}");

        let claims = ApprovalClaims {
            rules_decision: None,
            llm_recommendation: None,
            requested_decision: None,
            ..claims
        };
        assert!(verifier()
            .verify(&mint(TEST_SECRET, &claims), "assign", "VWCE-XETRA", "req-1", None)
            .is_ok());
    }

    #[test]
    fn a_token_whose_note_contradicts_its_own_hash_is_rejected() {
        let claims = ApprovalClaims {
            research_note: Some("approved research note".to_string()),
            research_note_sha256: research_note_hash(Some("a different note")),
            ..commit_claims()
        };
        let error = verify_commit(&claims).expect_err("must reject");
        assert!(error.contains("does not match its own signature"), "{error}");
    }

    #[test]
    fn a_token_with_a_matching_note_hash_verifies() {
        let note = "VWCE-XETRA scores 89/100 on cost, diversification and fund scale.";
        let claims = ApprovalClaims {
            research_note: Some(note.to_string()),
            research_note_sha256: research_note_hash(Some(note)),
            ..commit_claims()
        };
        let verified = verify_commit(&claims).expect("must verify");
        assert_eq!(verified.effective_research_note(), Some(note));
    }

    #[test]
    fn a_token_without_audit_identity_is_rejected() {
        for claims in [
            ApprovalClaims { actor_id: "  ".to_string(), ..commit_claims() },
            ApprovalClaims { nonce: String::new(), ..commit_claims() },
        ] {
            let error = verify_commit(&claims).expect_err("must reject");
            assert!(error.contains("audit identity"), "{error}");
        }
    }

    #[test]
    fn blank_free_text_claims_read_as_absent() {
        let claims = ApprovalClaims {
            research_note: Some("   \n ".to_string()),
            override_rationale: Some(String::new()),
            assignee: Some(" ".to_string()),
            ..commit_claims()
        };
        assert_eq!(claims.effective_research_note(), None);
        assert_eq!(claims.effective_override_rationale(), None);
        assert_eq!(claims.effective_assignee(), None);
    }

    /// The token binds the note by hash, so the hash must be stable and
    /// distinguish absent text from empty text.
    #[test]
    fn research_note_hash_is_stable_and_distinguishes_absence() {
        assert_eq!(research_note_hash(None), None);
        assert_ne!(research_note_hash(Some("")), None);
        assert_eq!(research_note_hash(Some("grounded note")), research_note_hash(Some("grounded note")));
        assert_ne!(research_note_hash(Some("grounded note")), research_note_hash(Some("grounded note ")));
    }
}
