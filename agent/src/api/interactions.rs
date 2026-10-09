//! `POST /executions/{execution_id}/interactions/{interaction_id}/response`
//!
//! The human's answer to an approval prompt. Mounted only when the approval
//! feature is enabled: a read-only deployment has no such route at all.
//!
//! The gateway has already authenticated the browser session, checked CSRF,
//! bounded the body and checked protocol-level consistency. This route checks
//! what only the agent can know: whether this pending prompt exists, belongs to
//! this user, and offered this answer (`approval::pending`).

use axum::{
    Extension,
    body::Bytes,
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
};
use serde::Deserialize;

use super::AppState;
use crate::{
    approval::pending::{HumanResponse, RespondError},
    error::ApiError,
    identity::TrustedCaller,
};

/// Same ceiling as the gateway's.
pub const MAX_INTERACTION_BODY: usize = 16 * 1024;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct InteractionRequest {
    response: HumanResponse,
}

pub async fn respond(
    State(state): State<AppState>,
    Extension(responder): Extension<TrustedCaller>,
    Path((execution_id, interaction_id)): Path<(String, String)>,
    body: Bytes,
) -> Result<Response, ApiError> {
    for (label, id) in [("execution", &execution_id), ("interaction", &interaction_id)] {
        if uuid::Uuid::parse_str(id).is_err() {
            return Err(ApiError::BadRequest(format!("invalid {label} ID")));
        }
    }
    if body.len() > MAX_INTERACTION_BODY {
        return Err(ApiError::BadRequest("interaction response is too large".into()));
    }
    let request: InteractionRequest = serde_json::from_slice(&body)
        .map_err(|error| ApiError::BadRequest(format!("invalid interaction response: {error}")))?;

    let span = tracing::info_span!(
        "human_approval.respond",
        execution.id = %execution_id,
        interaction.id = %interaction_id,
        interaction.outcome = tracing::field::Empty,
    );
    let result = state.services.interactions.respond(&execution_id, &interaction_id, &responder, &request.response);
    span.record(
        "interaction.outcome",
        match &result {
            Ok(()) => "accepted",
            Err(RespondError::NotFound) => "not_found",
            Err(RespondError::NotOwner) => "not_owner",
            Err(RespondError::Gone) => "gone",
            Err(_) => "not_offered",
        },
    );
    match result {
        // NAT answers an accepted interaction with 204; the UI expects it.
        Ok(()) => Ok(StatusCode::NO_CONTENT.into_response()),
        Err(error @ RespondError::NotFound) => Err(ApiError::NotFound(error.to_string())),
        Err(error @ RespondError::NotOwner) => {
            tracing::warn!(parent: &span, "rejected an interaction response from a user who does not own it");
            Err(ApiError::Forbidden(error.to_string()))
        }
        Err(error @ RespondError::Gone) => Err(ApiError::Gone(error.to_string())),
        Err(error) => Err(ApiError::Unprocessable(error.to_string())),
    }
}
