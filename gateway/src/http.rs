//! Routing and response hardening.

use axum::{
    extract::{Request, State},
    http::{
        header::{self, HeaderName, HeaderValue},
        StatusCode,
    },
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde_json::json;

use crate::auth;
use crate::error::GatewayError;
use crate::oidc::agent_reachable;
use crate::proxy;
use crate::state::AppState;

async fn health() -> impl IntoResponse {
    Json(json!({ "status": "ok" }))
}

async fn ready(State(state): State<AppState>) -> Result<impl IntoResponse, GatewayError> {
    let keycloak_ok = state.keycloak.discovery_reachable().await;
    let agent_ok = agent_reachable(
        &state.client,
        &state.config.agent_workflow_url,
        state.config.upstream_timeout,
    )
    .await;

    if keycloak_ok && agent_ok {
        Ok(Json(json!({ "status": "ready" })))
    } else {
        // Booleans only: this endpoint is unauthenticated, so it names which
        // dependency is unhappy and nothing about where it lives.
        Err(GatewayError::service_unavailable(format!(
            "dependencies unavailable: keycloak={keycloak_ok}, agent={agent_ok}"
        )))
    }
}

async fn security_headers(request: Request, next: Next) -> Response {
    let mut response = next.run(request).await;
    let headers = response.headers_mut();
    for (name, value) in [
        ("x-content-type-options", "nosniff"),
        ("referrer-policy", "no-referrer"),
        ("x-frame-options", "DENY"),
    ] {
        headers.insert(
            HeaderName::from_static(name),
            HeaderValue::from_static(value),
        );
    }
    // Streamed responses set their own no-cache directive; everything else must
    // not be stored at all.
    if !headers.contains_key(header::CACHE_CONTROL) {
        headers.insert(header::CACHE_CONTROL, HeaderValue::from_static("no-store"));
    }
    response
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/ready", get(ready))
        .route("/auth/login", get(auth::login))
        .route("/auth/callback", get(auth::callback))
        .route("/auth/session", get(auth::auth_session))
        .route("/auth/logout", post(auth::logout))
        .route("/api/chat", post(proxy::chat))
        .route(
            "/api/interactions/{execution_id}/{interaction_id}",
            post(proxy::interaction_response),
        )
        .fallback(not_found)
        .with_state(state)
        .layer(middleware::from_fn(security_headers))
}

async fn not_found() -> Response {
    (StatusCode::NOT_FOUND, Json(json!({ "error": "not found" }))).into_response()
}

#[cfg(test)]
mod tests {
    use axum::body::Body;
    use axum::http::Request as HttpRequest;
    use tower::ServiceExt;

    use super::*;

    fn state() -> AppState {
        use std::sync::Arc;
        let config = Arc::new(crate::config::test_support::config());
        let client = reqwest::Client::new();
        AppState {
            keycloak: Arc::new(crate::oidc::KeycloakClient::new(client.clone(), config.clone())),
            sessions: Arc::new(crate::session::SessionStore::new(config.max_sessions)),
            pending: Arc::new(crate::auth::PendingLoginStore::new(config.max_pending_logins)),
            client,
            config,
        }
    }

    async fn get(path: &str) -> Response {
        router(state())
            .oneshot(HttpRequest::builder().uri(path).body(Body::empty()).expect("request"))
            .await
            .expect("response")
    }

    #[tokio::test]
    async fn every_response_carries_the_hardening_headers() {
        let response = get("/health").await;
        assert_eq!(response.status(), StatusCode::OK);
        let headers = response.headers();
        assert_eq!(headers.get("x-content-type-options").unwrap(), "nosniff");
        assert_eq!(headers.get("referrer-policy").unwrap(), "no-referrer");
        assert_eq!(headers.get("x-frame-options").unwrap(), "DENY");
        assert_eq!(headers.get(header::CACHE_CONTROL).unwrap(), "no-store");
    }

    /// The gateway must expose only its own surface. A path that happens to exist
    /// on the agent must not appear to exist here.
    #[tokio::test]
    async fn unknown_paths_are_not_found_rather_than_proxied() {
        for path in ["/v1/workflow/full", "/mcp", "/executions/x/interactions/y/response", "/"] {
            let response = get(path).await;
            assert_eq!(response.status(), StatusCode::NOT_FOUND, "{path}");
            assert_eq!(
                response.headers().get("x-content-type-options").unwrap(),
                "nosniff",
                "{path} bypassed the hardening layer"
            );
        }
    }

    #[tokio::test]
    async fn protected_endpoints_reject_an_anonymous_caller() {
        let response = router(state())
            .oneshot(
                HttpRequest::builder()
                    .method("POST")
                    .uri("/api/chat")
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"messages":[{"role":"user","content":"hi"}]}"#))
                    .expect("request"),
            )
            .await
            .expect("response");
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
        assert_eq!(response.headers().get(header::WWW_AUTHENTICATE).unwrap(), "Session");
    }

    /// An anonymous session probe is a normal answer, not an error: the UI asks on
    /// every page load to decide whether to render the login button.
    #[tokio::test]
    async fn an_anonymous_session_probe_answers_normally() {
        let response = get("/auth/session").await;
        assert_eq!(response.status(), StatusCode::OK);
    }
}
