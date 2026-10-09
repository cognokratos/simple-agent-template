//! The agent-service HTTP contract (docs/AGENT-SERVICE-CONTRACT.md).
//!
//! | Route | Auth | Purpose |
//! | --- | --- | --- |
//! | `GET /health`, `/health/live` | none | liveness |
//! | `GET /health/ready` | none | readiness: MCP session open |
//! | `GET /version` | key + identity | provenance for evaluation |
//! | `POST /v1/workflow/full` | key + identity | the streaming workflow |
//! | `POST /executions/{e}/interactions/{i}/response` | key + identity | approval answers (only when enabled) |
//!
//! Every other path answers 401 without the key and 404 with it, so the
//! unauthenticated surface is liveness only — as on `main`.

pub mod auth;
pub mod interactions;
pub mod wire;
pub mod workflow;

use std::sync::Arc;

use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, State},
    http::StatusCode,
    middleware,
    response::IntoResponse,
    routing::{get, post},
};

use crate::{error::ApiError, services::Services};

#[derive(Clone)]
pub struct AppState {
    pub services: Arc<Services>,
}

/// Body ceiling for the whole service; the workflow route also bounds the
/// parsed conversation.
const MAX_BODY_BYTES: usize = 2 * 1024 * 1024;

pub fn router(services: Arc<Services>) -> Router {
    let key = auth::ServiceKey::new(&services.settings.gateway_api_key);
    let approvals = services.settings.approval.is_some();
    let state = AppState { services };

    let mut protected =
        Router::new().route("/version", get(version)).route("/v1/workflow/full", post(workflow::workflow_full));
    if approvals {
        protected = protected
            .route("/executions/{execution_id}/interactions/{interaction_id}/response", post(interactions::respond));
    }
    let protected = protected
        .fallback(not_found)
        .layer(DefaultBodyLimit::max(MAX_BODY_BYTES))
        // Outermost on the protected tree: nothing — not even a 404 — is
        // answered to a caller who is not the gateway.
        .layer(middleware::from_fn_with_state(key, auth::require_gateway))
        .with_state(state.clone());

    Router::new()
        .route("/health", get(live))
        .route("/health/live", get(live))
        .route("/health/ready", get(ready))
        .with_state(state)
        .fallback_service(protected)
}

async fn live() -> &'static str {
    "ok"
}

async fn ready(State(state): State<AppState>) -> impl IntoResponse {
    if state.services.mcp.is_connected().await {
        (StatusCode::OK, "ready")
    } else {
        (StatusCode::SERVICE_UNAVAILABLE, "mcp session closed")
    }
}

async fn version(State(state): State<AppState>) -> impl IntoResponse {
    Json(state.services.provenance.clone())
}

async fn not_found() -> ApiError {
    ApiError::NotFound("not found".into())
}
