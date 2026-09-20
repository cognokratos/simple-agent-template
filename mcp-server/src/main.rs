mod approval;
mod mutation;

use std::{net::SocketAddr, sync::Arc, time::Duration};

use anyhow::Context;
use axum::{
    extract::{Request, State},
    http::{header, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use chrono::{DateTime, Utc};
use rmcp::{
    handler::server::{router::tool::ToolRouter, wrapper::Parameters},
    model::*,
    tool, tool_handler, tool_router,
    transport::streamable_http_server::{
        session::local::LocalSessionManager, StreamableHttpServerConfig, StreamableHttpService,
    },
    ErrorData as McpError, ServerHandler,
};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use serde_json::json;

use crate::approval::{ApprovalVerifier, MIN_SECRET_LENGTH};
use crate::mutation::{ExecuteRequest, ExecuteResponse};
use sqlx::{postgres::PgPoolOptions, FromRow, PgPool};
use tokio_util::sync::CancellationToken;
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};

#[derive(Debug, Deserialize, JsonSchema)]
struct SearchTicketsArgs {
    /// Optional ticket status, for example "open" or "resolved".
    status: Option<String>,
    /// Maximum number of tickets to return. Defaults to 50 and is capped at 100.
    limit: Option<i64>,
}

#[derive(Debug, Deserialize, JsonSchema)]
struct GetTicketArgs {
    /// Exact ticket identifier, for example "TKT-1001".
    ticket_id: String,
}

#[derive(Debug, Clone, Serialize, FromRow)]
struct TicketSummary {
    id: String,
    subject: String,
    status: String,
    priority: String,
    customer_name: String,
    created_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Serialize, FromRow)]
struct TicketDetail {
    id: String,
    subject: String,
    status: String,
    priority: String,
    description: String,
    customer_name: String,
    order_reference: Option<String>,
    assigned_to: Option<String>,
    created_at: DateTime<Utc>,
    updated_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Serialize, FromRow)]
struct TicketEvent {
    id: String,
    ticket_id: String,
    occurred_at: DateTime<Utc>,
    event_type: String,
    author: String,
    summary: String,
}

#[derive(Clone)]
struct TicketsMcpServer {
    pool: PgPool,
    // Read by the `#[tool_handler]` expansion on the ServerHandler impl below,
    // which dead-code analysis does not attribute back to this field. Without
    // the allow, `cargo clippy -D warnings` fails on an entirely correct field.
    #[allow(dead_code)]
    tool_router: ToolRouter<Self>,
}

impl TicketsMcpServer {
    fn new(pool: PgPool) -> Self {
        Self {
            pool,
            tool_router: Self::tool_router(),
        }
    }

    fn database_error(error: sqlx::Error) -> McpError {
        tracing::error!(%error, "database operation failed");
        McpError::internal_error(
            "The tickets database operation failed".to_string(),
            Some(json!({ "cause": error.to_string() })),
        )
    }
}

#[tool_router]
impl TicketsMcpServer {
    #[tool(
        description = "Search support tickets using optional status and limit filters. Use status='open' when the user asks for open tickets. Returns ticket IDs and summary fields; call get_ticket for complete ticket details and history."
    )]
    async fn search_tickets(
        &self,
        Parameters(args): Parameters<SearchTicketsArgs>,
    ) -> Result<CallToolResult, McpError> {
        let limit = args.limit.unwrap_or(50).clamp(1, 100);
        let status = args.status.as_deref().map(str::trim).filter(|value| !value.is_empty());

        let tickets = if let Some(status) = status {
            sqlx::query_as::<_, TicketSummary>(
                r#"
                SELECT id, subject, status, priority, customer_name, created_at
                FROM tickets
                WHERE LOWER(status) = LOWER($1)
                ORDER BY created_at DESC
                LIMIT $2
                "#,
            )
            .bind(status)
            .bind(limit)
            .fetch_all(&self.pool)
            .await
            .map_err(Self::database_error)?
        } else {
            sqlx::query_as::<_, TicketSummary>(
                r#"
                SELECT id, subject, status, priority, customer_name, created_at
                FROM tickets
                ORDER BY created_at DESC
                LIMIT $1
                "#,
            )
            .bind(limit)
            .fetch_all(&self.pool)
            .await
            .map_err(Self::database_error)?
        };

        let response = json!({
            "count": tickets.len(),
            "filters": {
                "status": status,
                "limit": limit,
            },
            "tickets": tickets,
        });

        Ok(CallToolResult::success(vec![ContentBlock::text(
            response.to_string(),
        )]))
    }

    #[tool(
        description = "Get one support ticket by exact ticket_id, including its complete metadata and every associated history event (customer messages, shipping updates, support notes, refund-status updates). Use this after search_tickets when full history is required."
    )]
    async fn get_ticket(
        &self,
        Parameters(args): Parameters<GetTicketArgs>,
    ) -> Result<CallToolResult, McpError> {
        let ticket_id = args.ticket_id.trim();
        if ticket_id.is_empty() {
            return Err(McpError::invalid_params(
                "ticket_id must not be empty".to_string(),
                None,
            ));
        }

        let ticket = sqlx::query_as::<_, TicketDetail>(
            r#"
            SELECT id, subject, status, priority, description, customer_name,
                   order_reference, assigned_to, created_at, updated_at
            FROM tickets
            WHERE id = $1
            "#,
        )
        .bind(ticket_id)
        .fetch_optional(&self.pool)
        .await
        .map_err(Self::database_error)?;

        let Some(ticket) = ticket else {
            return Err(McpError::invalid_params(
                format!("Ticket '{ticket_id}' was not found"),
                Some(json!({ "ticket_id": ticket_id })),
            ));
        };

        let history = sqlx::query_as::<_, TicketEvent>(
            r#"
            SELECT id, ticket_id, occurred_at, event_type, author, summary
            FROM ticket_events
            WHERE ticket_id = $1
            ORDER BY occurred_at DESC
            "#,
        )
        .bind(ticket_id)
        .fetch_all(&self.pool)
        .await
        .map_err(Self::database_error)?;

        let response = json!({
            "ticket": ticket,
            "history_count": history.len(),
            "history": history,
        });

        Ok(CallToolResult::success(vec![ContentBlock::text(
            response.to_string(),
        )]))
    }
}

#[tool_handler]
impl ServerHandler for TicketsMcpServer {
    fn get_info(&self) -> ServerInfo {
        ServerInfo::new(ServerCapabilities::builder().enable_tools().build())
            .with_protocol_version(ProtocolVersion::V_2024_11_05)
            .with_server_info(Implementation::from_build_env())
            .with_instructions(
                "Use search_tickets to find ticket IDs. Use get_ticket for one ticket and its history. For history across all open tickets, first call search_tickets with status='open', then call get_ticket once for every returned ticket ID. Never invent ticket data."
                    .to_string(),
            )
    }
}

/// Apply the mutation an approval token authorizes.
///
/// Reached only by the agent, over the internal network, with the MCP service
/// credential -- and only when the approval feature is enabled. The token is the
/// authority: no mutation parameter is read from this request body except the
/// request id, which is checked *against* the token rather than trusted.
///
/// A refusal is a 200 with `ok: false`: a legitimately approved change can still
/// be refused by backend policy, and the caller has to be able to tell the user
/// plainly that nothing was applied. A 4xx is reserved for a token that should
/// never have been presented at all.
async fn execute_approval(
    State(state): State<ApprovalState>,
    Json(request): Json<ExecuteRequest>,
) -> Response {
    match mutation::execute(&state.pool, &state.verifier, &request).await {
        Ok(response) => (StatusCode::OK, Json(response)).into_response(),
        Err(reason) => {
            tracing::warn!(reason, "refused an approval token");
            (
                StatusCode::FORBIDDEN,
                Json(ExecuteResponse { ok: false, result: json!({ "error": reason }) }),
            )
                .into_response()
        }
    }
}

#[derive(Clone)]
struct ApprovalState {
    pool: PgPool,
    verifier: ApprovalVerifier,
}

async fn health() -> &'static str {
    "ok"
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    left.iter()
        .zip(right.iter())
        .fold(0_u8, |difference, (a, b)| difference | (a ^ b))
        == 0
}

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

    // Prevent the validated service credential from reaching RMCP logging or
    // any downstream request instrumentation.
    request.headers_mut().remove(header::AUTHORIZATION);
    next.run(request).await
}

#[tokio::main]
async fn main() {
    if let Err(error) = run().await {
        eprintln!("tickets MCP server failed: {error:#}");
        std::process::exit(1);
    }
}

async fn run() -> anyhow::Result<()> {
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "tickets_mcp_server=info,rmcp=info".into()),
        )
        .with(tracing_subscriber::fmt::layer())
        .init();

    tracing::info!("starting tickets MCP server");

    let database_url = std::env::var("DATABASE_URL")
        .context("DATABASE_URL must be configured")?;
    let api_key = std::env::var("MCP_API_KEY")
        .context("MCP_API_KEY must be configured")?;
    if api_key.trim().is_empty() {
        anyhow::bail!("MCP_API_KEY must not be empty");
    }
    let expected_authorization = Arc::new(format!("Bearer {api_key}"));
    let bind_address = std::env::var("MCP_BIND_ADDRESS")
        .unwrap_or_else(|_| "0.0.0.0:8080".to_string());
    let allowed_hosts = std::env::var("MCP_ALLOWED_HOSTS")
        .unwrap_or_else(|_| {
            "localhost,localhost:8080,127.0.0.1,127.0.0.1:8080,::1,mcp-server,mcp-server:8080"
                .to_string()
        })
        .split(',')
        .map(str::trim)
        .filter(|host| !host.is_empty())
        .map(ToOwned::to_owned)
        .collect::<Vec<_>>();

    if allowed_hosts.is_empty() {
        anyhow::bail!("MCP_ALLOWED_HOSTS must contain at least one host");
    }

    let pool = PgPoolOptions::new()
        .max_connections(10)
        .acquire_timeout(Duration::from_secs(10))
        .connect(&database_url)
        .await
        .context("failed to connect to Postgres")?;

    sqlx::query("SELECT 1")
        .execute(&pool)
        .await
        .context("Postgres health check failed")?;

    let cancellation = CancellationToken::new();
    let service_pool = pool.clone();
    let service = StreamableHttpService::new(
        move || Ok(TicketsMcpServer::new(service_pool.clone())),
        LocalSessionManager::default().into(),
        StreamableHttpServerConfig::default()
            // RMCP rejects non-loopback Host headers by default to protect
            // local servers from DNS rebinding. NAT connects through Docker
            // using Host: mcp-server:8080, so allow only the expected Docker
            // service names and local development hosts.
            .with_allowed_hosts(allowed_hosts.clone())
            .with_cancellation_token(cancellation.child_token()),
    );

    // The approval feature is enabled by the presence of its shared secret.
    // Absent secret means the route is never mounted, so a read-only deployment
    // has no mutation surface to attack rather than a disabled one.
    let approval_secret = match std::env::var("HITL_APPROVAL_SECRET") {
        Err(_) => None,
        Ok(value) if value.trim().is_empty() => None,
        Ok(value) => {
            anyhow::ensure!(
                value.len() >= MIN_SECRET_LENGTH,
                "HITL_APPROVAL_SECRET must contain at least {MIN_SECRET_LENGTH} characters"
            );
            Some(Arc::new(value.into_bytes()))
        }
    };

    let mut protected = Router::new().nest_service("/mcp", service);
    if mutation::approvals_enabled(approval_secret.as_deref())
        && let Some(secret) = approval_secret.clone()
    {
        protected = protected.merge(
            Router::new()
                .route("/approvals/execute", post(execute_approval))
                .with_state(ApprovalState {
                    pool: pool.clone(),
                    verifier: ApprovalVerifier::new(secret),
                }),
        );
        tracing::info!(
            actions = ?mutation::ACTIONS.iter().map(|a| a.name).collect::<Vec<_>>(),
            "human-approval execution endpoint enabled"
        );
    } else {
        tracing::info!(
            "HITL_APPROVAL_SECRET is not set; this server is read-only and exposes \
             no approval execution endpoint"
        );
    }

    // One authentication layer over both, so the approval endpoint can never be
    // reached without the MCP service credential.
    let protected_mcp = protected.route_layer(middleware::from_fn_with_state(
        expected_authorization,
        require_api_key,
    ));
    let app = Router::new()
        .route("/health", get(health))
        .merge(protected_mcp);
    let address: SocketAddr = bind_address.parse().context("invalid MCP_BIND_ADDRESS")?;
    let listener = tokio::net::TcpListener::bind(address)
        .await
        .context("failed to bind MCP server")?;

    tracing::info!(%address, ?allowed_hosts, "tickets MCP server listening");
    axum::serve(listener, app)
        .with_graceful_shutdown(async move {
            let _ = tokio::signal::ctrl_c().await;
            cancellation.cancel();
        })
        .await
        .context("MCP HTTP server failed")?;

    Ok(())
}
