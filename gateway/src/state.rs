//! Shared application state.

use std::sync::Arc;

use reqwest::Client;

use crate::auth::PendingLoginStore;
use crate::config::GatewayConfig;
use crate::oidc::KeycloakClient;
use crate::session::SessionStore;

#[derive(Clone)]
pub struct AppState {
    pub config: Arc<GatewayConfig>,
    pub client: Client,
    pub keycloak: Arc<KeycloakClient>,
    pub sessions: Arc<SessionStore>,
    pub pending: Arc<PendingLoginStore>,
}
