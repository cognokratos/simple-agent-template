use std::{net::SocketAddr, time::Duration};

use anyhow::Context;
use axum::{routing::get, Router};
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
use sqlx::{postgres::PgPoolOptions, FromRow, PgPool};
use tokio_util::sync::CancellationToken;
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};

#[derive(Debug, Deserialize, JsonSchema)]
struct SearchAlertsArgs {
    /// Optional alert status, for example "open" or "closed".
    status: Option<String>,
    /// Maximum number of alerts to return. Defaults to 50 and is capped at 100.
    limit: Option<i64>,
}

#[derive(Debug, Deserialize, JsonSchema)]
struct GetAlertArgs {
    /// Exact alert identifier, for example "ALT-1001".
    alert_id: String,
}

#[derive(Debug, Clone, Serialize, FromRow)]
struct AlertSummary {
    id: String,
    title: String,
    status: String,
    severity: String,
    customer_name: String,
    created_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Serialize, FromRow)]
struct AlertDetail {
    id: String,
    title: String,
    status: String,
    severity: String,
    description: String,
    customer_name: String,
    assigned_to: Option<String>,
    created_at: DateTime<Utc>,
}

#[derive(Debug, Clone, Serialize, FromRow)]
struct Transaction {
    id: String,
    alert_id: String,
    occurred_at: DateTime<Utc>,
    amount: f64,
    currency: String,
    direction: String,
    counterparty: String,
    country: String,
    risk_score: i32,
    description: String,
}

#[derive(Clone)]
struct AlertsMcpServer {
    pool: PgPool,
    tool_router: ToolRouter<Self>,
}

impl AlertsMcpServer {
    fn new(pool: PgPool) -> Self {
        Self {
            pool,
            tool_router: Self::tool_router(),
        }
    }

    fn database_error(error: sqlx::Error) -> McpError {
        tracing::error!(%error, "database operation failed");
        McpError::internal_error(
            "The alerts database operation failed".to_string(),
            Some(json!({ "cause": error.to_string() })),
        )
    }
}

#[tool_router]
impl AlertsMcpServer {
    #[tool(
        description = "Search alerts using optional status and limit filters. Use status='open' when the user asks for open alerts. Returns alert IDs and summary fields; call get_alert for complete alert details and transactions."
    )]
    async fn search_alerts(
        &self,
        Parameters(args): Parameters<SearchAlertsArgs>,
    ) -> Result<CallToolResult, McpError> {
        let limit = args.limit.unwrap_or(50).clamp(1, 100);
        let status = args.status.as_deref().map(str::trim).filter(|value| !value.is_empty());

        let alerts = if let Some(status) = status {
            sqlx::query_as::<_, AlertSummary>(
                r#"
                SELECT id, title, status, severity, customer_name, created_at
                FROM alerts
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
            sqlx::query_as::<_, AlertSummary>(
                r#"
                SELECT id, title, status, severity, customer_name, created_at
                FROM alerts
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
            "count": alerts.len(),
            "filters": {
                "status": status,
                "limit": limit,
            },
            "alerts": alerts,
        });

        Ok(CallToolResult::success(vec![ContentBlock::text(
            response.to_string(),
        )]))
    }

    #[tool(
        description = "Get one alert by exact alert_id, including its complete metadata and every associated transaction. Use this after search_alerts when transaction details are required."
    )]
    async fn get_alert(
        &self,
        Parameters(args): Parameters<GetAlertArgs>,
    ) -> Result<CallToolResult, McpError> {
        let alert_id = args.alert_id.trim();
        if alert_id.is_empty() {
            return Err(McpError::invalid_params(
                "alert_id must not be empty".to_string(),
                None,
            ));
        }

        let alert = sqlx::query_as::<_, AlertDetail>(
            r#"
            SELECT id, title, status, severity, description, customer_name,
                   assigned_to, created_at
            FROM alerts
            WHERE id = $1
            "#,
        )
        .bind(alert_id)
        .fetch_optional(&self.pool)
        .await
        .map_err(Self::database_error)?;

        let Some(alert) = alert else {
            return Err(McpError::invalid_params(
                format!("Alert '{alert_id}' was not found"),
                Some(json!({ "alert_id": alert_id })),
            ));
        };

        let transactions = sqlx::query_as::<_, Transaction>(
            r#"
            SELECT id, alert_id, occurred_at, amount, currency, direction,
                   counterparty, country, risk_score, description
            FROM transactions
            WHERE alert_id = $1
            ORDER BY occurred_at DESC
            "#,
        )
        .bind(alert_id)
        .fetch_all(&self.pool)
        .await
        .map_err(Self::database_error)?;

        let response = json!({
            "alert": alert,
            "transaction_count": transactions.len(),
            "transactions": transactions,
        });

        Ok(CallToolResult::success(vec![ContentBlock::text(
            response.to_string(),
        )]))
    }
}

#[tool_handler]
impl ServerHandler for AlertsMcpServer {
    fn get_info(&self) -> ServerInfo {
        ServerInfo::new(ServerCapabilities::builder().enable_tools().build())
            .with_protocol_version(ProtocolVersion::V_2024_11_05)
            .with_server_info(Implementation::from_build_env())
            .with_instructions(
                "Use search_alerts to find alert IDs. Use get_alert for one alert and its transactions. For all transactions belonging to open alerts, first call search_alerts with status='open', then call get_alert once for every returned alert ID. Never invent alert data."
                    .to_string(),
            )
    }
}

async fn health() -> &'static str {
    "ok"
}

#[tokio::main]
async fn main() {
    if let Err(error) = run().await {
        eprintln!("alerts MCP server failed: {error:#}");
        std::process::exit(1);
    }
}

async fn run() -> anyhow::Result<()> {
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "alerts_mcp_server=info,rmcp=info".into()),
        )
        .with(tracing_subscriber::fmt::layer())
        .init();

    tracing::info!("starting alerts MCP server");

    let database_url = std::env::var("DATABASE_URL")
        .context("DATABASE_URL must be configured")?;
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
        move || Ok(AlertsMcpServer::new(service_pool.clone())),
        LocalSessionManager::default().into(),
        StreamableHttpServerConfig::default()
            // RMCP rejects non-loopback Host headers by default to protect
            // local servers from DNS rebinding. NAT connects through Docker
            // using Host: mcp-server:8080, so allow only the expected Docker
            // service names and local development hosts.
            .with_allowed_hosts(allowed_hosts.clone())
            .with_cancellation_token(cancellation.child_token()),
    );

    let app = Router::new()
        .route("/health", get(health))
        .nest_service("/mcp", service);
    let address: SocketAddr = bind_address.parse().context("invalid MCP_BIND_ADDRESS")?;
    let listener = tokio::net::TcpListener::bind(address)
        .await
        .context("failed to bind MCP server")?;

    tracing::info!(%address, ?allowed_hosts, "alerts MCP server listening");
    axum::serve(listener, app)
        .with_graceful_shutdown(async move {
            let _ = tokio::signal::ctrl_c().await;
            cancellation.cancel();
        })
        .await
        .context("MCP HTTP server failed")?;

    Ok(())
}
