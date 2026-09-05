//! HTTP surface: the MCP transport, the internal approval-execution endpoint,
//! and the shared-secret gate in front of both.

use std::sync::Arc;

use axum::{
    extract::{Request, State},
    http::{header, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Router,
};
use rmcp::{
    handler::server::wrapper::Parameters,
    model::CallToolResult,
    transport::streamable_http_server::{
        session::local::LocalSessionManager, StreamableHttpServerConfig, StreamableHttpService,
    },
};
use serde::Deserialize;
use serde_json::{json, Value};
use tokio_util::sync::CancellationToken;

use crate::server::{AssignEtfArgs, CommitEvaluationArgs, EtfMcpServer, ShortlistEtfArgs};

#[derive(Clone)]
struct HttpState {
    /// One server instance for the approval endpoint. It used to be rebuilt per
    /// request, which regenerated the JSON schema for every tool on each approval.
    server: Arc<EtfMcpServer>,
}

#[derive(Debug, Deserialize)]
struct ExecuteApprovalRequest {
    approval_token: String,
    request_id: String,
}

/// Flatten a tool result into the JSON envelope the agent expects.
fn tool_result_json(result: CallToolResult) -> Value {
    let text = result
        .content
        .first()
        .and_then(|block| block.as_text().map(|t| t.text.clone()))
        .unwrap_or_default();
    let payload: Value = serde_json::from_str(&text).unwrap_or(Value::String(text));
    json!({ "ok": result.is_error != Some(true), "result": payload })
}

/// Execute the mutation an approval authorizes.
///
/// The action and the ETF are read from the signed token, so the caller supplies
/// only the approval reference. This exists so the human's confirmation and the
/// resulting state change are a single step: otherwise the agent has to make one
/// more model turn to call the mutation itself, and an empty completion on that
/// turn leaves the candidate approved but unchanged. Every check still runs —
/// signature, lifetime, payload binding, recomputed evaluation, hard constraints,
/// the locked-row state precondition and the one-time nonce.
async fn execute_approval(
    State(state): State<HttpState>,
    axum::Json(request): axum::Json<ExecuteApprovalRequest>,
) -> Result<Response, StatusCode> {
    let server = state.server;
    let claims = server
        .approvals()
        .decode(&request.approval_token)
        .map_err(|_| StatusCode::BAD_REQUEST)?;

    let etf_id = claims.etf_id.clone();
    let token = request.approval_token;
    let request_id = request.request_id;

    let outcome = match claims.action.as_str() {
        "commit" => {
            server
                .commit_evaluation(Parameters(CommitEvaluationArgs {
                    etf_id,
                    approval_token: token,
                    request_id,
                }))
                .await
        }
        "shortlist" => {
            server
                .shortlist_etf(Parameters(ShortlistEtfArgs {
                    etf_id,
                    approval_token: token,
                    request_id,
                }))
                .await
        }
        "assign" => {
            server
                .assign_etf(Parameters(AssignEtfArgs {
                    etf_id,
                    approval_token: token,
                    request_id,
                }))
                .await
        }
        _ => return Err(StatusCode::BAD_REQUEST),
    };

    match outcome {
        Ok(result) => Ok(axum::Json(tool_result_json(result)).into_response()),
        Err(error) => {
            // A rejected mutation is a refusal, not a server fault. Replayed
            // nonces and invalid payloads land here and must stay diagnosable.
            tracing::warn!(%error, "approved mutation was rejected");
            Ok((StatusCode::CONFLICT, axum::Json(json!({ "ok": false, "error": error.message })))
                .into_response())
        }
    }
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    left.iter().zip(right.iter()).fold(0_u8, |difference, (a, b)| difference | (a ^ b)) == 0
}

/// Gate every non-health route on the internal shared secret, and strip the
/// credential before the request reaches a handler.
async fn require_api_key(
    State(expected): State<Arc<String>>,
    mut request: Request,
    next: Next,
) -> Response {
    let provided = request
        .headers()
        .get(header::AUTHORIZATION)
        .map(|value| value.as_bytes())
        .unwrap_or_default();
    if !constant_time_eq(provided, expected.as_bytes()) {
        return (
            StatusCode::UNAUTHORIZED,
            [(header::WWW_AUTHENTICATE, "Bearer")],
            "invalid or missing internal API key",
        )
            .into_response();
    }
    request.headers_mut().remove(header::AUTHORIZATION);
    next.run(request).await
}

async fn health() -> &'static str {
    "ok"
}

/// Assemble the application: `/health` open, everything else behind the key.
pub fn router(
    server: Arc<EtfMcpServer>,
    api_key: &str,
    allowed_hosts: Vec<String>,
    cancellation: &CancellationToken,
) -> Router {
    let mcp_server = server.clone();
    let mcp = StreamableHttpService::new(
        // rmcp builds one handler per session, so this cannot share the Arc above.
        move || Ok((*mcp_server).clone()),
        LocalSessionManager::default().into(),
        StreamableHttpServerConfig::default()
            .with_allowed_hosts(allowed_hosts)
            .with_cancellation_token(cancellation.child_token()),
    );

    let expected_authorization = Arc::new(format!("Bearer {api_key}"));
    let protected = Router::new()
        .nest_service("/mcp", mcp)
        .route("/approvals/execute", post(execute_approval))
        .with_state(HttpState { server })
        .route_layer(middleware::from_fn_with_state(expected_authorization, require_api_key));

    Router::new().route("/health", get(health)).merge(protected)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn credential_comparison_rejects_mismatches_of_every_shape() {
        assert!(constant_time_eq(b"Bearer secret", b"Bearer secret"));
        assert!(!constant_time_eq(b"Bearer secret", b"Bearer secreT"));
        assert!(!constant_time_eq(b"", b"Bearer secret"));
        assert!(!constant_time_eq(b"Bearer secret", b""));
        // A prefix must never pass; length is compared before the bytes.
        assert!(!constant_time_eq(b"Bearer sec", b"Bearer secret"));
        assert!(constant_time_eq(b"", b""));
    }

    #[test]
    fn tool_results_are_flattened_with_their_success_flag() {
        let success = tool_result_json(CallToolResult::success(vec![
            rmcp::model::ContentBlock::text(r#"{"etf_id":"VWCE-XETRA"}"#.to_string()),
        ]));
        assert_eq!(success["ok"], json!(true));
        assert_eq!(success["result"]["etf_id"], json!("VWCE-XETRA"));

        let refusal = tool_result_json(CallToolResult::error(vec![
            rmcp::model::ContentBlock::text("Hard constraint HC-UCITS".to_string()),
        ]));
        assert_eq!(refusal["ok"], json!(false));
        assert_eq!(refusal["result"], json!("Hard constraint HC-UCITS"));
    }
}
