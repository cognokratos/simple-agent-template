//! HTTP error boundary.
//!
//! Every error that leaves this service is one of a few classes with a fixed
//! status and a short JSON body. Bodies never echo a credential, a header
//! value, or an upstream error string: the gateway logs agent rejections
//! verbatim, and an upstream message can describe internal topology.

use axum::{
    Json,
    http::{HeaderValue, StatusCode, header},
    response::{IntoResponse, Response},
};
use serde_json::json;

/// The error classes of the agent-service contract (docs/AGENT-SERVICE-CONTRACT.md).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ApiError {
    /// Not the gateway: missing or wrong service credential. Same body as `main`.
    InvalidServiceKey,
    /// The gateway did not assert exactly one identity. Same body as `main`.
    MissingIdentity,
    /// The request does not fit the schema or its bounds.
    BadRequest(String),
    /// The responder is authenticated but not the user the prompt was for.
    Forbidden(String),
    /// No such pending interaction (never created, already answered, expired).
    NotFound(String),
    /// The pending interaction's execution is gone (cancelled, disconnected).
    Gone(String),
    /// A well-formed response that this prompt never offered.
    Unprocessable(String),
    /// The route exists only when an optional feature is enabled.
    FeatureDisabled,
    /// Startup or dependency state that makes the request unservable.
    Unavailable(String),
}

impl ApiError {
    pub fn status(&self) -> StatusCode {
        match self {
            Self::InvalidServiceKey | Self::MissingIdentity => StatusCode::UNAUTHORIZED,
            Self::BadRequest(_) => StatusCode::BAD_REQUEST,
            Self::Forbidden(_) => StatusCode::FORBIDDEN,
            Self::NotFound(_) | Self::FeatureDisabled => StatusCode::NOT_FOUND,
            Self::Gone(_) => StatusCode::GONE,
            Self::Unprocessable(_) => StatusCode::UNPROCESSABLE_ENTITY,
            Self::Unavailable(_) => StatusCode::SERVICE_UNAVAILABLE,
        }
    }

    pub fn message(&self) -> &str {
        match self {
            Self::InvalidServiceKey => "invalid or missing internal API key",
            Self::MissingIdentity => "missing or ambiguous authenticated identity",
            Self::FeatureDisabled => "not found",
            Self::BadRequest(message)
            | Self::Forbidden(message)
            | Self::NotFound(message)
            | Self::Gone(message)
            | Self::Unprocessable(message)
            | Self::Unavailable(message) => message,
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let mut response = (self.status(), Json(json!({ "error": self.message() }))).into_response();
        if self == Self::InvalidServiceKey {
            response.headers_mut().insert(header::WWW_AUTHENTICATE, HeaderValue::from_static("Bearer"));
        }
        response
    }
}
