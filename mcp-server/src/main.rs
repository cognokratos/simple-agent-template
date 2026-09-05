//! ETF research MCP server: a deterministic ETF evaluation engine behind an MCP
//! surface, where every state change requires a signed human approval and lands
//! in an append-only history.
//!
//! Module map:
//!
//! * [`rules`] — the deterministic engine, the specification it interprets, and
//!   the decision vocabulary. No I/O.
//! * [`domain`] — stored records and the read models handed back to clients.
//! * [`approval`] — human approval tokens and their verification. No I/O.
//! * [`store`] — every SQL statement this service issues.
//! * [`server`] — the MCP tools and resources, and the mutation invariants.
//! * [`http`] — transport, the internal approval endpoint, and the auth gate.
//! * [`seed`] — loading the shipped ETF snapshot.

use std::{sync::Arc, time::Duration};

use anyhow::Context as _;
use sqlx::postgres::PgPoolOptions;
use tokio_util::sync::CancellationToken;
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};

mod approval;
mod domain;
mod http;
mod rules;
mod seed;
mod server;
mod store;

#[cfg(test)]
mod fixtures;

const DEFAULT_BIND_ADDRESS: &str = "0.0.0.0:8080";
const DEFAULT_ALLOWED_HOSTS: &str =
    "localhost,localhost:8080,127.0.0.1,127.0.0.1:8080,::1,mcp-server,mcp-server:8080";
const MIN_APPROVAL_SECRET_LENGTH: usize = 24;

#[tokio::main]
async fn main() {
    if let Err(error) = run().await {
        eprintln!("ETF research MCP server failed: {error:#}");
        std::process::exit(1);
    }
}

/// Everything this service reads from its environment, validated once at startup
/// so a misconfiguration fails at boot rather than on the first request.
struct Config {
    database_url: String,
    api_key: String,
    approval_secret: Vec<u8>,
    rules_path: String,
    profile_path: String,
    etfs_path: String,
    bind_address: String,
    allowed_hosts: Vec<String>,
}

impl Config {
    fn from_env() -> anyhow::Result<Self> {
        let database_url =
            std::env::var("DATABASE_URL").context("DATABASE_URL must be configured")?;
        let api_key = std::env::var("MCP_API_KEY").context("MCP_API_KEY must be configured")?;
        let approval_secret = std::env::var("HITL_APPROVAL_SECRET")
            .context("HITL_APPROVAL_SECRET must be configured")?;

        anyhow::ensure!(!api_key.trim().is_empty(), "MCP_API_KEY must not be empty");
        anyhow::ensure!(
            approval_secret.len() >= MIN_APPROVAL_SECRET_LENGTH,
            "HITL_APPROVAL_SECRET must be at least {MIN_APPROVAL_SECRET_LENGTH} characters"
        );

        let allowed_hosts = std::env::var("MCP_ALLOWED_HOSTS")
            .unwrap_or_else(|_| DEFAULT_ALLOWED_HOSTS.to_string())
            .split(',')
            .map(str::trim)
            .filter(|host| !host.is_empty())
            .map(ToOwned::to_owned)
            .collect::<Vec<_>>();
        anyhow::ensure!(
            !allowed_hosts.is_empty(),
            "MCP_ALLOWED_HOSTS must list at least one host"
        );

        Ok(Self {
            database_url,
            api_key,
            approval_secret: approval_secret.into_bytes(),
            rules_path: std::env::var("RULES_SPEC_PATH")
                .unwrap_or_else(|_| "/app/data/rules_spec.json".to_string()),
            profile_path: std::env::var("INVESTOR_PROFILE_PATH")
                .unwrap_or_else(|_| "/app/data/investor_profile.json".to_string()),
            etfs_path: std::env::var("ETFS_DATA_PATH")
                .unwrap_or_else(|_| "/app/data/etfs.json".to_string()),
            bind_address: std::env::var("MCP_BIND_ADDRESS")
                .unwrap_or_else(|_| DEFAULT_BIND_ADDRESS.to_string()),
            allowed_hosts,
        })
    }
}

async fn run() -> anyhow::Result<()> {
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "etf_mcp_server=info,rmcp=info".into()),
        )
        .with(tracing_subscriber::fmt::layer())
        .init();

    let config = Config::from_env()?;
    // Validation happens at boot: an inconsistent specification — weights that do
    // not sum to 100, decision bands with a gap, a metric naming a field that does
    // not exist — must stop the service rather than silently change every score.
    let rules = rules::RulesSpec::parse(
        &std::fs::read_to_string(&config.rules_path)
            .with_context(|| format!("failed to read {}", config.rules_path))?,
    )
    .map_err(anyhow::Error::msg)?;
    let profile: rules::InvestorProfile = serde_json::from_str(
        &std::fs::read_to_string(&config.profile_path)
            .with_context(|| format!("failed to read {}", config.profile_path))?,
    )
    .context("investor_profile.json is invalid")?;
    let rules = Arc::new(rules);
    let profile = Arc::new(profile);

    let pool = PgPoolOptions::new()
        .max_connections(10)
        .acquire_timeout(Duration::from_secs(10))
        .connect(&config.database_url)
        .await
        .context("failed to connect to Postgres")?;
    sqlx::query("SELECT 1")
        .execute(&pool)
        .await
        .context("Postgres health check failed")?;
    seed::seed_etfs(&pool, &rules, &profile, &config.etfs_path).await?;

    let cancellation = CancellationToken::new();
    let mcp = Arc::new(server::EtfMcpServer::new(
        pool,
        rules,
        profile,
        Arc::new(config.approval_secret),
    ));
    let app = http::router(mcp, &config.api_key, config.allowed_hosts.clone(), &cancellation);

    let address = config
        .bind_address
        .parse::<std::net::SocketAddr>()
        .context("invalid MCP_BIND_ADDRESS")?;
    let listener = tokio::net::TcpListener::bind(address)
        .await
        .context("failed to bind MCP server")?;
    tracing::info!(
        %address,
        allowed_hosts = ?config.allowed_hosts,
        "ETF research MCP server listening"
    );

    axum::serve(listener, app)
        .with_graceful_shutdown(async move {
            let _ = tokio::signal::ctrl_c().await;
            cancellation.cancel();
        })
        .await
        .context("MCP HTTP server failed")?;
    Ok(())
}
