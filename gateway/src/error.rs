//! Client-facing errors.
//!
//! Each variant carries exactly what the browser is told. Upstream detail —
//! Keycloak error bodies, reqwest errors that embed `http://keycloak:8080` or
//! `http://agent:8000` — goes to the log and never into the response. This is an
//! unauthenticated boundary, and those strings describe internal topology.

use std::fmt::Display;

use axum::{
    http::{header, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use serde_json::json;

#[derive(Debug)]
pub struct GatewayError {
    pub status: StatusCode,
    pub message: String,
}

impl GatewayError {
    fn new(status: StatusCode, message: impl Into<String>) -> Self {
        Self { status, message: message.into() }
    }

    pub fn unauthorized(message: impl Into<String>) -> Self {
        Self::new(StatusCode::UNAUTHORIZED, message)
    }

    pub fn forbidden(message: impl Into<String>) -> Self {
        Self::new(StatusCode::FORBIDDEN, message)
    }

    pub fn bad_request(message: impl Into<String>) -> Self {
        Self::new(StatusCode::BAD_REQUEST, message)
    }

    pub fn too_many_requests(message: impl Into<String>) -> Self {
        Self::new(StatusCode::TOO_MANY_REQUESTS, message)
    }

    pub fn service_unavailable(message: impl Into<String>) -> Self {
        Self::new(StatusCode::SERVICE_UNAVAILABLE, message)
    }

    /// A dependency failed. The cause is logged with its context and the caller
    /// learns only which dependency, never why.
    pub fn upstream(dependency: &str, cause: impl Display) -> Self {
        tracing::warn!(dependency, %cause, "upstream dependency call failed");
        Self::new(
            StatusCode::BAD_GATEWAY,
            format!("the {dependency} service is unavailable"),
        )
    }

    /// A dependency rejected us. Same discipline as [`Self::upstream`], but the
    /// caller is unauthenticated rather than the system being broken.
    pub fn upstream_rejected(dependency: &str, cause: impl Display) -> Self {
        tracing::warn!(dependency, %cause, "upstream dependency rejected the request");
        Self::new(
            StatusCode::UNAUTHORIZED,
            "authentication could not be completed",
        )
    }

    /// An invariant this service was supposed to uphold did not hold.
    pub fn internal(context: &str, cause: impl Display) -> Self {
        tracing::error!(context, %cause, "gateway internal error");
        Self::new(StatusCode::INTERNAL_SERVER_ERROR, "internal gateway error")
    }
}

impl IntoResponse for GatewayError {
    fn into_response(self) -> Response {
        let mut response = (self.status, Json(json!({ "error": self.message }))).into_response();
        if self.status == StatusCode::UNAUTHORIZED {
            response
                .headers_mut()
                .insert(header::WWW_AUTHENTICATE, HeaderValue::from_static("Session"));
        }
        response
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn upstream_errors_never_carry_their_cause_to_the_client() {
        let error = GatewayError::upstream(
            "keycloak",
            "error sending request for url (http://keycloak:8080/realms/etf-research/protocol/openid-connect/token)",
        );
        assert_eq!(error.status, StatusCode::BAD_GATEWAY);
        assert!(!error.message.contains("keycloak:8080"), "{}", error.message);
        assert!(!error.message.contains("realms"), "{}", error.message);
    }

    #[test]
    fn rejected_upstream_authentication_reads_as_unauthorized() {
        let error = GatewayError::upstream_rejected("keycloak", "401: {\"error\":\"invalid_grant\"}");
        assert_eq!(error.status, StatusCode::UNAUTHORIZED);
        assert!(!error.message.contains("invalid_grant"), "{}", error.message);
    }
}
