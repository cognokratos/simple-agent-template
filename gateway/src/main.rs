//! ETF research authentication gateway.
//!
//! A backend-for-frontend between the browser and the agent: it owns the OIDC
//! authorization-code flow, holds the tokens so the browser never sees them, and
//! forwards the user's identity to the agent as trusted HTTP metadata.
//!
//! Module map:
//!
//! * [`config`] — environment, validated once at startup.
//! * [`error`] — what the caller is told, and what only the log sees.
//! * [`cookies`] — the three browser cookies this service owns.
//! * [`session`] — sessions and the write-back discipline that keeps logout real.
//! * [`oidc`] — Keycloak: authorization, tokens, JWKS, userinfo.
//! * [`auth`] — the login flow and session refresh.
//! * [`proxy`] — authenticated pass-through to the agent.
//! * [`http`] — routing and response hardening.

use std::{net::SocketAddr, sync::Arc, time::Duration};

use anyhow::{Context, Result};
use reqwest::Client;
use tracing::{info, warn};
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};

mod auth;
mod config;
mod cookies;
mod error;
mod http;
mod oidc;
mod proxy;
mod session;
mod state;

use crate::config::GatewayConfig;
use crate::state::AppState;

#[tokio::main]
async fn main() {
    if let Err(error) = run().await {
        eprintln!("ETF research authentication gateway failed: {error:#}");
        std::process::exit(1);
    }
}

/// Reclaim expired sessions and abandoned logins.
///
/// Both stores also prune on write, so this only bounds the memory held by state
/// nobody comes back for.
async fn cleanup_expired(state: AppState) {
    let mut interval = tokio::time::interval(Duration::from_secs(60));
    loop {
        interval.tick().await;
        state.pending.drop_expired().await;
        state.sessions.drop_expired().await;
    }
}

async fn run() -> Result<()> {
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "etf_auth_gateway=info,tower_http=info".into()),
        )
        .with(tracing_subscriber::fmt::layer())
        .init();

    let config = Arc::new(GatewayConfig::from_env()?);
    // No global request timeout: the proxied chat response is a long-lived event
    // stream. Every other upstream call sets its own, from `upstream_timeout`.
    let client = Client::builder()
        .connect_timeout(Duration::from_secs(10))
        .pool_idle_timeout(Duration::from_secs(90))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .context("failed to build HTTP client")?;

    let state = AppState {
        keycloak: Arc::new(oidc::KeycloakClient::new(client.clone(), config.clone())),
        sessions: Arc::new(session::SessionStore::new(config.max_sessions)),
        pending: Arc::new(auth::PendingLoginStore::new(config.max_pending_logins)),
        client,
        config: config.clone(),
    };
    tokio::spawn(cleanup_expired(state.clone()));

    let address: SocketAddr = config
        .bind_address
        .parse()
        .context("GATEWAY_BIND_ADDRESS is invalid")?;
    let listener = tokio::net::TcpListener::bind(address)
        .await
        .context("failed to bind authentication gateway")?;
    info!(%address, "ETF research authentication gateway listening");

    axum::serve(listener, http::router(state))
        .with_graceful_shutdown(async {
            if let Err(error) = tokio::signal::ctrl_c().await {
                warn!(%error, "could not install shutdown signal");
            }
        })
        .await
        .context("authentication gateway server failed")?;
    Ok(())
}
