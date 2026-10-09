//! Caller authentication: "is this the gateway?" then "who is it acting for?".
//!
//! Two questions, two checks, in that order — the same layering as
//! `fastapi_worker.py` on `main`:
//!
//! 1. **Service credential.** Network reachability answers "can this packet
//!    arrive"; it cannot answer "is this caller the gateway". This service
//!    *trusts* the identity headers it receives — they end up in approval
//!    tokens and the audit trail — so it must authenticate whoever sends them.
//!    Compared in constant time, then removed from the request so no handler,
//!    log line or span can observe it.
//! 2. **Asserted identity.** Exactly one non-empty `x-authenticated-user-id`.
//!    A repeated header is ambiguous, not a list to take the first element of:
//!    accepting the first would let anything able to append a header decide
//!    who the user is.
//!
//! The result is a [`TrustedCaller`] in the request extensions. Handlers read
//! identity from there and nowhere else.

use axum::{
    extract::{Request, State},
    http::{HeaderMap, header},
    middleware::Next,
    response::{IntoResponse, Response},
};
use subtle::ConstantTimeEq;

use crate::{
    error::ApiError,
    identity::{IDENTITY_HEADER, MAX_IDENTITY_CHARS, REQUEST_ID_HEADER, ROLES_HEADER, TrustedCaller, USERNAME_HEADER},
};

/// The only unauthenticated paths: liveness and readiness, as on `main`.
pub const PUBLIC_PATHS: [&str; 3] = ["/health", "/health/live", "/health/ready"];

/// The expected `Authorization` value, held once.
#[derive(Clone)]
pub struct ServiceKey(std::sync::Arc<Vec<u8>>);

impl ServiceKey {
    pub fn new(api_key: &str) -> Self {
        Self(std::sync::Arc::new(format!("Bearer {api_key}").into_bytes()))
    }

    fn matches(&self, provided: &[u8]) -> bool {
        // `ct_eq` on slices of different lengths returns false without
        // comparing contents; the length of a fixed-format key is not a secret.
        provided.ct_eq(&self.0).into()
    }
}

/// Axum middleware applied to every non-public route.
pub async fn require_gateway(State(key): State<ServiceKey>, mut request: Request, next: Next) -> Response {
    let provided: Vec<u8> =
        request.headers().get(header::AUTHORIZATION).map(|value| value.as_bytes().to_vec()).unwrap_or_default();
    if request.headers().get_all(header::AUTHORIZATION).iter().count() > 1 || !key.matches(&provided) {
        tracing::warn!(path = %request.uri().path(), "rejected a request without the gateway service credential");
        return ApiError::InvalidServiceKey.into_response();
    }
    // Validated: nothing downstream may observe the credential.
    request.headers_mut().remove(header::AUTHORIZATION);

    match caller_from_headers(request.headers()) {
        Ok(caller) => {
            request.extensions_mut().insert(caller);
            next.run(request).await
        }
        Err(error) => {
            tracing::warn!(path = %request.uri().path(), "rejected a request without one unambiguous asserted identity");
            error.into_response()
        }
    }
}

/// Exactly one occurrence, non-empty after trimming, bounded, printable.
fn sole_header(headers: &HeaderMap, name: &str) -> Result<Option<String>, ()> {
    let mut values = headers.get_all(name).iter();
    let Some(first) = values.next() else { return Ok(None) };
    if values.next().is_some() {
        return Err(());
    }
    let text = first.to_str().map_err(|_| ())?.trim();
    if text.is_empty() {
        return Ok(None);
    }
    if text.chars().count() > MAX_IDENTITY_CHARS || text.chars().any(char::is_control) {
        return Err(());
    }
    Ok(Some(text.to_string()))
}

/// Build the trusted caller from gateway headers. Separate from the
/// middleware so the whole rejection matrix is testable without HTTP.
pub fn caller_from_headers(headers: &HeaderMap) -> Result<TrustedCaller, ApiError> {
    let user_id = match sole_header(headers, IDENTITY_HEADER) {
        Ok(Some(user_id)) => user_id,
        Ok(None) | Err(()) => return Err(ApiError::MissingIdentity),
    };
    let username = sole_header(headers, USERNAME_HEADER).map_err(|()| ApiError::MissingIdentity)?;
    let roles = sole_header(headers, ROLES_HEADER)
        .map_err(|()| ApiError::MissingIdentity)?
        .map(|roles| {
            roles.split(',').map(str::trim).filter(|role| !role.is_empty()).take(64).map(str::to_string).collect()
        })
        .unwrap_or_default();
    // A repeated or malformed request id is refused rather than replaced: the
    // id is what an approval token is bound to.
    let request_id =
        sole_header(headers, REQUEST_ID_HEADER).map_err(|()| ApiError::BadRequest("invalid x-request-id".into()))?;
    if let Some(id) = &request_id
        && (id.len() > 128 || !id.bytes().all(|byte| byte.is_ascii_graphic()))
    {
        return Err(ApiError::BadRequest("invalid x-request-id".into()));
    }
    Ok(TrustedCaller::from_verified_headers(user_id, username, roles, request_id))
}

#[cfg(test)]
mod tests {
    use axum::http::HeaderValue;

    use super::*;
    use crate::identity::RequestIdSource;

    fn headers(pairs: &[(&'static str, &str)]) -> HeaderMap {
        let mut map = HeaderMap::new();
        for (name, value) in pairs {
            map.append(*name, HeaderValue::from_str(value).unwrap());
        }
        map
    }

    #[test]
    fn a_single_identity_header_is_trusted() {
        let caller = caller_from_headers(&headers(&[
            (IDENTITY_HEADER, "user-1"),
            (ROLES_HEADER, "support, reviewer"),
            (REQUEST_ID_HEADER, "11111111-1111-4111-8111-111111111111"),
        ]))
        .unwrap();
        assert_eq!(caller.user_id(), "user-1");
        assert_eq!(caller.roles(), ["support", "reviewer"]);
        assert_eq!(caller.request_id_source(), RequestIdSource::Gateway);
    }

    #[test]
    fn missing_empty_or_repeated_identity_is_refused() {
        assert_eq!(caller_from_headers(&headers(&[])), Err(ApiError::MissingIdentity));
        assert_eq!(caller_from_headers(&headers(&[(IDENTITY_HEADER, "   ")])), Err(ApiError::MissingIdentity));
        assert_eq!(
            caller_from_headers(&headers(&[(IDENTITY_HEADER, "a"), (IDENTITY_HEADER, "b")])),
            Err(ApiError::MissingIdentity)
        );
    }

    #[test]
    fn an_oversized_identity_is_refused() {
        let long = "x".repeat(MAX_IDENTITY_CHARS + 1);
        assert_eq!(caller_from_headers(&headers(&[(IDENTITY_HEADER, &long)])), Err(ApiError::MissingIdentity));
    }

    #[test]
    fn a_missing_request_id_is_generated_and_marked_as_such() {
        let caller = caller_from_headers(&headers(&[(IDENTITY_HEADER, "evaluation-harness")])).unwrap();
        assert_eq!(caller.request_id_source(), RequestIdSource::Generated);
        assert!(caller.gateway_request_id().is_none());
    }

    #[test]
    fn a_repeated_request_id_is_refused() {
        let result = caller_from_headers(&headers(&[
            (IDENTITY_HEADER, "u"),
            (REQUEST_ID_HEADER, "a"),
            (REQUEST_ID_HEADER, "b"),
        ]));
        assert!(matches!(result, Err(ApiError::BadRequest(_))));
    }

    #[test]
    fn the_service_key_comparison_is_exact() {
        let key = ServiceKey::new("secret");
        assert!(key.matches(b"Bearer secret"));
        for wrong in [&b"Bearer secre"[..], b"Bearer secrets", b"bearer secret", b"secret", b""] {
            assert!(!key.matches(wrong), "{wrong:?}");
        }
    }
}
