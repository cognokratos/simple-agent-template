//! The Keycloak side of the BFF: authorization URLs, code exchange, token
//! refresh, userinfo, and ID-token validation.
//!
//! Two asymmetries here are deliberate and easy to get backwards. Tokens, JWKS
//! and userinfo are fetched over the *internal* URL, because that is where the
//! service can reach Keycloak; but the issuer an ID token claims is the *public*
//! URL, because that is the one the browser was redirected to. Validating a token
//! against the internal issuer would reject every genuine login.
//!
//! Every call here carries an explicit timeout. `connect_timeout` alone covers
//! only TCP setup, so a Keycloak that accepts the connection and then stalls used
//! to hang the request — and with refresh sitting inside session authentication,
//! that meant hanging every authenticated request indefinitely.

use std::time::{Duration, Instant};

use jsonwebtoken::{decode, decode_header, jwk::JwkSet, Algorithm, DecodingKey, Validation};
use reqwest::Client;
use serde::Deserialize;
use serde_json::Value;
use tokio::sync::RwLock;
use url::Url;

use crate::config::GatewayConfig;
use crate::error::GatewayError;
use crate::session::{constant_time_eq, UserIdentity};

/// How long a fetched key set is reused before being refreshed.
const JWKS_CACHE_TTL: Duration = Duration::from_secs(300);
/// Floor between forced refetches when a token names an unknown key, so an
/// attacker cannot turn bogus `kid` values into unbounded load on Keycloak.
const JWKS_REFETCH_FLOOR: Duration = Duration::from_secs(30);

#[derive(Debug, Deserialize)]
pub struct TokenResponse {
    pub access_token: String,
    pub expires_in: u64,
    pub refresh_token: Option<String>,
    pub id_token: Option<String>,
}

#[derive(Debug, Deserialize)]
struct UserInfoResponse {
    sub: String,
    preferred_username: Option<String>,
    email: Option<String>,
    name: Option<String>,
    realm_access: Option<RealmAccess>,
}

#[derive(Debug, Deserialize)]
struct RealmAccess {
    #[serde(default)]
    roles: Vec<String>,
}

/// `iss`, `aud` and `exp` are never read directly — `Validation` checks them —
/// but declaring them non-optional makes deserialization fail on a token that
/// omits any of them, which is a second, independent guard.
#[derive(Debug, Deserialize)]
#[allow(dead_code)]
struct IdTokenClaims {
    iss: String,
    aud: Value,
    exp: u64,
    nonce: Option<String>,
    sub: String,
}

/// A refresh either renews the session or tells us Keycloak has ended it. The
/// distinction matters: only an outright rejection may revoke a session, and a
/// transport failure must never be mistaken for one.
pub enum RefreshOutcome {
    Renewed(Box<TokenResponse>),
    Rejected,
}

pub struct KeycloakClient {
    client: Client,
    config: std::sync::Arc<GatewayConfig>,
    jwks: RwLock<Option<CachedJwks>>,
}

struct CachedJwks {
    keys: JwkSet,
    fetched_at: Instant,
}

impl KeycloakClient {
    pub fn new(client: Client, config: std::sync::Arc<GatewayConfig>) -> Self {
        Self { client, config, jwks: RwLock::new(None) }
    }

    fn internal(&self, suffix: &str) -> String {
        self.config.realm_endpoint(&self.config.keycloak_internal_url, suffix)
    }

    fn public(&self, suffix: &str) -> String {
        self.config.realm_endpoint(&self.config.keycloak_public_url, suffix)
    }

    /// The issuer an ID token from this realm must claim: the browser-facing URL.
    fn expected_issuer(&self) -> String {
        self.public("").trim_end_matches('/').to_owned()
    }

    pub fn authorization_url(
        &self,
        oauth_state: &str,
        nonce: &str,
        code_challenge: &str,
    ) -> Result<Url, GatewayError> {
        let mut url = Url::parse(&self.public("protocol/openid-connect/auth"))
            .map_err(|error| GatewayError::internal("authorization URL", error))?;
        url.query_pairs_mut()
            .append_pair("client_id", &self.config.oidc_client_id)
            .append_pair("response_type", "code")
            .append_pair("redirect_uri", self.config.callback_url())
            .append_pair("scope", "openid profile email roles")
            .append_pair("state", oauth_state)
            .append_pair("nonce", nonce)
            .append_pair("code_challenge", code_challenge)
            .append_pair("code_challenge_method", "S256");
        Ok(url)
    }

    pub fn logout_url(&self, id_token: Option<&str>) -> Result<Url, GatewayError> {
        let mut url = Url::parse(&self.public("protocol/openid-connect/logout"))
            .map_err(|error| GatewayError::internal("logout URL", error))?;
        url.query_pairs_mut()
            .append_pair("client_id", &self.config.oidc_client_id)
            .append_pair("post_logout_redirect_uri", &self.config.ui_public_url);
        if let Some(id_token) = id_token {
            url.query_pairs_mut().append_pair("id_token_hint", id_token);
        }
        Ok(url)
    }

    pub async fn exchange_code(
        &self,
        code: &str,
        verifier: &str,
    ) -> Result<TokenResponse, GatewayError> {
        let response = self
            .client
            .post(self.internal("protocol/openid-connect/token"))
            .timeout(self.config.upstream_timeout)
            .form(&[
                ("grant_type", "authorization_code"),
                ("client_id", self.config.oidc_client_id.as_str()),
                ("client_secret", self.config.oidc_client_secret.as_str()),
                ("code", code),
                ("redirect_uri", self.config.callback_url()),
                ("code_verifier", verifier),
            ])
            .send()
            .await
            .map_err(|error| GatewayError::upstream("keycloak", error))?;

        let status = response.status();
        if !status.is_success() {
            let detail = response.text().await.unwrap_or_default();
            return Err(GatewayError::upstream_rejected(
                "keycloak",
                format!("token exchange returned {status}: {detail}"),
            ));
        }
        response
            .json::<TokenResponse>()
            .await
            .map_err(|error| GatewayError::upstream("keycloak", error))
    }

    pub async fn refresh(&self, refresh_token: &str) -> Result<RefreshOutcome, GatewayError> {
        let response = self
            .client
            .post(self.internal("protocol/openid-connect/token"))
            .timeout(self.config.upstream_timeout)
            .form(&[
                ("grant_type", "refresh_token"),
                ("client_id", self.config.oidc_client_id.as_str()),
                ("client_secret", self.config.oidc_client_secret.as_str()),
                ("refresh_token", refresh_token),
            ])
            .send()
            .await
            .map_err(|error| GatewayError::upstream("keycloak", error))?;

        let status = response.status();
        if !status.is_success() {
            // Keycloak answering "no" is the only thing that may end a session.
            // A 5xx is Keycloak being broken, not the user being logged out.
            if status.is_client_error() {
                tracing::info!(%status, "keycloak rejected a refresh token");
                return Ok(RefreshOutcome::Rejected);
            }
            return Err(GatewayError::upstream(
                "keycloak",
                format!("refresh returned {status}"),
            ));
        }
        response
            .json::<TokenResponse>()
            .await
            .map(|token| RefreshOutcome::Renewed(Box::new(token)))
            .map_err(|error| GatewayError::upstream("keycloak", error))
    }

    pub async fn user_info(&self, access_token: &str) -> Result<UserIdentity, GatewayError> {
        let response = self
            .client
            .get(self.internal("protocol/openid-connect/userinfo"))
            .timeout(self.config.upstream_timeout)
            .bearer_auth(access_token)
            .send()
            .await
            .map_err(|error| GatewayError::upstream("keycloak", error))?;
        if !response.status().is_success() {
            return Err(GatewayError::upstream_rejected(
                "keycloak",
                format!("userinfo returned {}", response.status()),
            ));
        }
        let user = response
            .json::<UserInfoResponse>()
            .await
            .map_err(|error| GatewayError::upstream("keycloak", error))?;
        Ok(UserIdentity {
            id: user.sub,
            username: user.preferred_username.unwrap_or_else(|| "unknown".to_owned()),
            email: user.email,
            name: user.name,
            roles: user.realm_access.map(|access| access.roles).unwrap_or_default(),
        })
    }

    pub async fn discovery_reachable(&self) -> bool {
        self.client
            .get(self.internal(".well-known/openid-configuration"))
            .timeout(self.config.upstream_timeout)
            .send()
            .await
            .map(|response| response.status().is_success())
            .unwrap_or(false)
    }

    async fn cached_key(&self, kid: &str) -> Result<Option<DecodingKey>, GatewayError> {
        let cached = self.jwks.read().await;
        let Some(cached) = cached.as_ref() else { return Ok(None) };
        if cached.fetched_at.elapsed() > JWKS_CACHE_TTL {
            return Ok(None);
        }
        let Some(jwk) = cached.keys.find(kid) else { return Ok(None) };
        DecodingKey::from_jwk(jwk)
            .map(Some)
            .map_err(|error| GatewayError::upstream("keycloak", error))
    }

    async fn fetch_key(&self, kid: &str) -> Result<DecodingKey, GatewayError> {
        {
            // Refetching on an unknown key is what makes key rotation work without
            // a restart, but an attacker can name any `kid` they like, so it is
            // rate-limited rather than done on demand.
            let cached = self.jwks.read().await;
            if let Some(cached) = cached.as_ref()
                && cached.fetched_at.elapsed() < JWKS_REFETCH_FLOOR
            {
                return Err(GatewayError::unauthorized("ID token signing key was not found"));
            }
        }

        let response = self
            .client
            .get(self.internal("protocol/openid-connect/certs"))
            .timeout(self.config.upstream_timeout)
            .send()
            .await
            .map_err(|error| GatewayError::upstream("keycloak", error))?;
        if !response.status().is_success() {
            return Err(GatewayError::upstream(
                "keycloak",
                format!("JWKS endpoint returned {}", response.status()),
            ));
        }
        let keys = response
            .json::<JwkSet>()
            .await
            .map_err(|error| GatewayError::upstream("keycloak", error))?;

        let key = keys
            .find(kid)
            .ok_or_else(|| GatewayError::unauthorized("ID token signing key was not found"))
            .and_then(|jwk| {
                DecodingKey::from_jwk(jwk)
                    .map_err(|_| GatewayError::unauthorized("unsupported ID token signing key"))
            });
        *self.jwks.write().await = Some(CachedJwks { keys, fetched_at: Instant::now() });
        key
    }

    /// Verify an ID token's signature, issuer, audience, expiry and nonce.
    pub async fn validate_id_token(
        &self,
        id_token: &str,
        expected_nonce: &str,
    ) -> Result<(), GatewayError> {
        let header = decode_header(id_token)
            .map_err(|_| GatewayError::unauthorized("invalid ID token header"))?;
        if header.alg != Algorithm::RS256 {
            return Err(GatewayError::unauthorized("unexpected ID token signing algorithm"));
        }
        let kid = header
            .kid
            .as_deref()
            .ok_or_else(|| GatewayError::unauthorized("ID token key ID is missing"))?;

        let decoding_key = match self.cached_key(kid).await? {
            Some(key) => key,
            None => self.fetch_key(kid).await?,
        };

        let mut validation = Validation::new(Algorithm::RS256);
        validation.set_audience(&[self.config.oidc_client_id.as_str()]);
        validation.set_issuer(&[self.expected_issuer().as_str()]);
        validation.set_required_spec_claims(&["exp", "iss", "aud", "sub"]);
        validation.validate_exp = true;
        let token = decode::<IdTokenClaims>(id_token, &decoding_key, &validation)
            .map_err(|error| {
                tracing::warn!(%error, "ID token validation failed");
                GatewayError::unauthorized("ID token validation failed")
            })?;

        let nonce = token
            .claims
            .nonce
            .ok_or_else(|| GatewayError::unauthorized("ID token nonce is missing"))?;
        if !constant_time_eq(&nonce, expected_nonce) {
            return Err(GatewayError::unauthorized("ID token nonce mismatch"));
        }
        if token.claims.sub.trim().is_empty() {
            return Err(GatewayError::unauthorized("ID token subject is missing"));
        }
        Ok(())
    }
}

/// Whether the agent's health endpoint reports ready.
pub async fn agent_reachable(client: &Client, workflow_url: &str, timeout: Duration) -> bool {
    client
        .get(agent_health_url(workflow_url))
        .timeout(timeout)
        .send()
        .await
        .map(|response| response.status().is_success())
        .unwrap_or(false)
}

pub fn agent_health_url(workflow_url: &str) -> String {
    Url::parse(workflow_url)
        .map(|mut url| {
            url.set_path("/health");
            url.set_query(None);
            url.to_string()
        })
        .unwrap_or_else(|_| "http://agent:8000/health".to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::test_support::config;

    #[test]
    fn the_agent_health_url_keeps_the_origin_and_drops_path_and_query() {
        assert_eq!(
            agent_health_url("http://agent:8000/v1/workflow/full?filter_steps=X"),
            "http://agent:8000/health"
        );
        assert_eq!(agent_health_url("https://agent.internal/v1/x"), "https://agent.internal/health");
        // An unparseable URL must still yield something usable rather than panic.
        assert_eq!(agent_health_url("not a url"), "http://agent:8000/health");
    }

    /// The issuer must be the browser-facing URL, never the internal one the
    /// service actually dials. Getting this backwards rejects every real login.
    #[test]
    fn the_expected_issuer_is_the_public_realm_url() {
        let config = std::sync::Arc::new(config());
        let keycloak = KeycloakClient::new(Client::new(), config);
        assert_eq!(keycloak.expected_issuer(), "http://localhost:8082/realms/etf-research");
        assert!(
            keycloak.internal("protocol/openid-connect/token").starts_with("http://keycloak:8080/"),
            "tokens are fetched over the internal URL"
        );
    }

    #[test]
    fn the_authorization_url_carries_pkce_and_the_exact_callback() {
        let config = std::sync::Arc::new(config());
        let keycloak = KeycloakClient::new(Client::new(), config.clone());
        let url = keycloak.authorization_url("state-1", "nonce-1", "challenge-1").expect("built");
        let pairs: std::collections::HashMap<_, _> = url.query_pairs().into_owned().collect();

        assert_eq!(pairs.get("response_type").map(String::as_str), Some("code"));
        assert_eq!(pairs.get("code_challenge_method").map(String::as_str), Some("S256"));
        assert_eq!(pairs.get("code_challenge").map(String::as_str), Some("challenge-1"));
        assert_eq!(pairs.get("state").map(String::as_str), Some("state-1"));
        assert_eq!(pairs.get("nonce").map(String::as_str), Some("nonce-1"));
        assert_eq!(pairs.get("redirect_uri").map(String::as_str), Some(config.callback_url()));
        assert!(url.as_str().starts_with("http://localhost:8082/"), "{url}");
    }

    #[test]
    fn the_logout_url_only_carries_an_id_token_hint_when_there_is_one() {
        let keycloak = KeycloakClient::new(Client::new(), std::sync::Arc::new(config()));
        let without = keycloak.logout_url(None).expect("built");
        assert!(!without.as_str().contains("id_token_hint"), "{without}");
        let with = keycloak.logout_url(Some("token")).expect("built");
        assert!(with.as_str().contains("id_token_hint=token"), "{with}");
    }
}
