//! Trusted identity: who the gateway says the user is.
//!
//! SECURITY-BOUNDARY: identity enters this service in exactly one place —
//! [`crate::api::auth`], from gateway-minted `x-authenticated-*` headers on a
//! request that has already proved, with the service credential, that it came
//! from the gateway. See docs/concepts/07-security-and-trust-boundaries.md.
//!
//! [`TrustedCaller`] has no public constructor. Nothing the model produces —
//! prompt text, tool arguments, a claimed approval — can become one, because
//! the only code path that builds it reads HTTP headers, and the model never
//! writes HTTP headers. The model never *sees* one either: it is not rendered
//! into the prompt, and no tool schema has a field for it.

use std::fmt;

/// The gateway-asserted identity header. Exactly one occurrence is accepted.
pub const IDENTITY_HEADER: &str = "x-authenticated-user-id";
pub const USERNAME_HEADER: &str = "x-authenticated-username";
pub const ROLES_HEADER: &str = "x-authenticated-roles";
pub const EMAIL_HEADER: &str = "x-authenticated-email";
pub const REQUEST_ID_HEADER: &str = "x-request-id";

/// Every gateway-minted identity header has an explicit telemetry decision,
/// as on `main` (`trace_processor.IDENTITY_HEADERS` / `RETAINED_GATEWAY_HEADERS`).
/// `scripts/verify_security_sources.py` fails if the gateway starts minting an
/// `x-authenticated-*` header that is in neither list.
///
/// Never exported, in any mode. Per-user attribution, when enabled, is a
/// pseudonym ([`TrustedCaller::pseudonym`]), never one of these values.
pub const IDENTITY_HEADERS: &[&str] = &[IDENTITY_HEADER, USERNAME_HEADER, EMAIL_HEADER];
/// Exported on the root span: a handful of shared values that explain an
/// authorisation outcome without identifying a person.
pub const RETAINED_GATEWAY_HEADERS: &[&str] = &[ROLES_HEADER];

/// Upper bound on any identity value. The gateway's own values are far
/// shorter; this only stops an absurd header becoming an absurd audit record.
pub const MAX_IDENTITY_CHARS: usize = 256;

/// Where the request id came from. An approval may only be bound to a request
/// id the *gateway* minted: a locally generated one correlates traces, but it
/// names no authenticated request the MCP server could check a token against.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RequestIdSource {
    Gateway,
    Generated,
}

/// The authenticated end user and the request they made.
#[derive(Clone, PartialEq, Eq)]
pub struct TrustedCaller {
    user_id: String,
    username: Option<String>,
    roles: Vec<String>,
    request_id: String,
    request_id_source: RequestIdSource,
}

impl TrustedCaller {
    /// Built only by the authentication layer, after the service credential
    /// and the identity header have both been verified.
    pub(crate) fn from_verified_headers(
        user_id: String,
        username: Option<String>,
        roles: Vec<String>,
        request_id: Option<String>,
    ) -> Self {
        let (request_id, request_id_source) = match request_id {
            Some(id) => (id, RequestIdSource::Gateway),
            None => (uuid::Uuid::new_v4().to_string(), RequestIdSource::Generated),
        };
        Self { user_id, username, roles, request_id, request_id_source }
    }

    /// The stable subject the gateway took from the validated session. This is
    /// what an approval token's `actor_id` — and so the audit trail — names.
    pub fn user_id(&self) -> &str {
        &self.user_id
    }

    pub fn username(&self) -> Option<&str> {
        self.username.as_deref()
    }

    pub fn roles(&self) -> &[String] {
        &self.roles
    }

    pub fn request_id(&self) -> &str {
        &self.request_id
    }

    pub fn request_id_source(&self) -> RequestIdSource {
        self.request_id_source
    }

    /// The request id, only if the gateway minted it. See [`RequestIdSource`].
    pub fn gateway_request_id(&self) -> Option<&str> {
        (self.request_id_source == RequestIdSource::Gateway).then_some(self.request_id.as_str())
    }

    /// A stable pseudonym for per-user trace attribution, exported only when
    /// `OTEL_TRACE_USER_ID=true`. Derived the same *way* NAT 1.9 derives
    /// `Context.user_id` (a UUIDv5 over the header name and value), but in this
    /// service's own namespace, so it is not the same value as on `main`.
    pub fn pseudonym(&self) -> String {
        const NAMESPACE: uuid::Uuid = uuid::Uuid::from_u128(0x8c5a_4b1e_7f3d_4e2a_9b6c_1d0e_f2a3_b4c5);
        let name = format!("trusted-header:{IDENTITY_HEADER}\u{1f}{}", self.user_id);
        uuid::Uuid::new_v5(&NAMESPACE, name.as_bytes()).to_string()
    }

    /// Test-only constructor, so tests can build a caller without HTTP.
    #[cfg(test)]
    pub fn for_tests(user_id: &str, request_id: Option<&str>) -> Self {
        Self::from_verified_headers(
            user_id.to_string(),
            None,
            vec!["support".to_string()],
            request_id.map(str::to_string),
        )
    }
}

/// Never prints the subject: a `{:?}` in a log line must not become a
/// per-user record. Use [`TrustedCaller::pseudonym`] where attribution is
/// explicitly enabled.
impl fmt::Debug for TrustedCaller {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("TrustedCaller")
            .field("user_id", &"[redacted]")
            .field("roles", &self.roles)
            .field("request_id", &self.request_id)
            .field("request_id_source", &self.request_id_source)
            .finish()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_identity_header_has_one_telemetry_decision() {
        for header in [IDENTITY_HEADER, USERNAME_HEADER, ROLES_HEADER, EMAIL_HEADER] {
            let decisions = IDENTITY_HEADERS.contains(&header) as u8 + RETAINED_GATEWAY_HEADERS.contains(&header) as u8;
            assert_eq!(decisions, 1, "{header}");
        }
    }

    #[test]
    fn debug_output_never_contains_the_subject() {
        let caller = TrustedCaller::for_tests("8f14e45f-subject", Some("req-1"));
        assert!(!format!("{caller:?}").contains("8f14e45f"));
    }

    #[test]
    fn only_a_gateway_request_id_can_bind_an_approval() {
        let gateway = TrustedCaller::for_tests("u", Some("req-1"));
        assert_eq!(gateway.gateway_request_id(), Some("req-1"));
        let generated = TrustedCaller::for_tests("u", None);
        assert_eq!(generated.request_id_source(), RequestIdSource::Generated);
        assert_eq!(generated.gateway_request_id(), None);
    }

    #[test]
    fn the_pseudonym_is_stable_and_not_the_subject() {
        let one = TrustedCaller::for_tests("alice", Some("a"));
        let two = TrustedCaller::for_tests("alice", Some("b"));
        let other = TrustedCaller::for_tests("bob", Some("a"));
        assert_eq!(one.pseudonym(), two.pseudonym());
        assert_ne!(one.pseudonym(), other.pseudonym());
        assert!(!one.pseudonym().contains("alice"));
    }
}
