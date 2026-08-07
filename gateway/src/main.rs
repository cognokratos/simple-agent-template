use std::{
    collections::HashMap,
    net::SocketAddr,
    sync::Arc,
    time::{Duration, Instant},
};

use anyhow::{Context, Result};
use axum::{
    body::{Body, Bytes},
    extract::{Query, Request, State},
    http::{
        header::{self, HeaderName, HeaderValue},
        HeaderMap, StatusCode,
    },
    middleware::{self, Next},
    response::{IntoResponse, Redirect, Response},
    routing::{get, post},
    Json, Router,
};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use futures_util::TryStreamExt;
use jsonwebtoken::{decode, decode_header, jwk::JwkSet, Algorithm, DecodingKey, Validation};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tokio::sync::RwLock;
use tracing::{info, warn};
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};
use url::Url;
use uuid::Uuid;

const DEFAULT_SESSION_COOKIE: &str = "alerts_gateway_session";
const DEFAULT_CSRF_COOKIE: &str = "alerts_gateway_csrf";
const DEFAULT_LOGIN_COOKIE: &str = "alerts_gateway_login";
const BROWSER_COOKIE_PATH: &str = "/api/gateway";
const OIDC_CALLBACK_PATH: &str = "/api/gateway/auth/callback";

#[derive(Clone)]
struct AppState {
    config: Arc<GatewayConfig>,
    client: Client,
    sessions: Arc<RwLock<HashMap<String, Session>>>,
    pending: Arc<RwLock<HashMap<String, PendingLogin>>>,
}

#[derive(Clone)]
struct GatewayConfig {
    bind_address: String,
    ui_public_url: String,
    oidc_callback_url: String,
    keycloak_public_url: String,
    keycloak_internal_url: String,
    keycloak_realm: String,
    oidc_client_id: String,
    oidc_client_secret: String,
    agent_workflow_url: String,
    agent_api_key: String,
    agent_filter_steps: String,
    cookie_secure: bool,
    session_ttl: Duration,
    max_sessions: usize,
    max_pending_logins: usize,
    max_messages: usize,
    max_message_chars: usize,
    max_total_message_chars: usize,
    session_cookie: String,
    csrf_cookie: String,
    login_cookie: String,
}

#[derive(Clone)]
struct PendingLogin {
    state: String,
    nonce: String,
    verifier: String,
    return_to: String,
    created_at: Instant,
}

#[derive(Clone)]
struct Session {
    user: UserIdentity,
    refresh_token: Option<String>,
    id_token: Option<String>,
    csrf_token: String,
    access_expires_at: Instant,
    session_expires_at: Instant,
}

#[derive(Clone, Debug, Serialize)]
struct UserIdentity {
    id: String,
    username: String,
    email: Option<String>,
    name: Option<String>,
    roles: Vec<String>,
}

#[derive(Debug, Deserialize)]
struct LoginQuery {
    return_to: Option<String>,
}

#[derive(Debug, Deserialize)]
struct CallbackQuery {
    code: Option<String>,
    state: Option<String>,
    error: Option<String>,
    error_description: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ChatProxyRequest {
    messages: Vec<ChatProxyMessage>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ChatProxyMessage {
    role: String,
    content: String,
}

#[derive(Debug, Deserialize)]
struct TokenResponse {
    access_token: String,
    expires_in: u64,
    refresh_token: Option<String>,
    id_token: Option<String>,
}

#[derive(Debug, Deserialize)]
struct UserInfo {
    sub: String,
    preferred_username: Option<String>,
    email: Option<String>,
    name: Option<String>,
    realm_access: Option<RealmAccess>,
}

#[derive(Debug, Deserialize)]
struct IdTokenClaims {
    iss: String,
    aud: Value,
    exp: u64,
    nonce: Option<String>,
    sub: String,
}

#[derive(Debug, Deserialize)]
struct RealmAccess {
    #[serde(default)]
    roles: Vec<String>,
}

#[derive(Debug)]
struct GatewayError {
    status: StatusCode,
    message: String,
}

impl GatewayError {
    fn unauthorized(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::UNAUTHORIZED,
            message: message.into(),
        }
    }

    fn forbidden(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::FORBIDDEN,
            message: message.into(),
        }
    }

    fn bad_gateway(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::BAD_GATEWAY,
            message: message.into(),
        }
    }

    fn bad_request(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            message: message.into(),
        }
    }

    fn too_many_requests(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::TOO_MANY_REQUESTS,
            message: message.into(),
        }
    }
}

impl IntoResponse for GatewayError {
    fn into_response(self) -> Response {
        let mut response = (self.status, Json(json!({ "error": self.message }))).into_response();
        if self.status == StatusCode::UNAUTHORIZED {
            response.headers_mut().insert(
                header::WWW_AUTHENTICATE,
                HeaderValue::from_static("Session"),
            );
        }
        response
    }
}

impl GatewayConfig {
    fn from_env() -> Result<Self> {
        let session_ttl_seconds = env_u64("GATEWAY_SESSION_TTL_SECONDS", 28_800)?;
        let config = Self {
            bind_address: env_or("GATEWAY_BIND_ADDRESS", "0.0.0.0:8081"),
            ui_public_url: env_or("UI_PUBLIC_URL", "http://localhost:3000"),
            oidc_callback_url: env_or(
                "OIDC_CALLBACK_URL",
                "http://localhost:3000/api/gateway/auth/callback",
            ),
            keycloak_public_url: env_or("KEYCLOAK_PUBLIC_URL", "http://localhost:8082"),
            keycloak_internal_url: env_or("KEYCLOAK_INTERNAL_URL", "http://keycloak:8080"),
            keycloak_realm: env_or("KEYCLOAK_REALM", "alerts"),
            oidc_client_id: env_or("KEYCLOAK_GATEWAY_CLIENT_ID", "alerts-gateway"),
            oidc_client_secret: required_env("KEYCLOAK_GATEWAY_CLIENT_SECRET")?,
            agent_workflow_url: env_or(
                "AGENT_WORKFLOW_URL",
                "http://agent:8000/v1/workflow/full",
            ),
            agent_api_key: required_env("AGENT_API_KEY")?,
            agent_filter_steps: env_or(
                "AGENT_FILTER_STEPS",
                "TOOL_START,TOOL_END,FUNCTION_START,FUNCTION_END",
            ),
            cookie_secure: env_bool("GATEWAY_COOKIE_SECURE", false),
            session_ttl: Duration::from_secs(session_ttl_seconds),
            max_sessions: env_usize("GATEWAY_MAX_SESSIONS", 1_000)?,
            max_pending_logins: env_usize("GATEWAY_MAX_PENDING_LOGINS", 1_000)?,
            max_messages: env_usize("GATEWAY_MAX_MESSAGES", 100)?,
            max_message_chars: env_usize("GATEWAY_MAX_MESSAGE_CHARS", 32_768)?,
            max_total_message_chars: env_usize("GATEWAY_MAX_TOTAL_MESSAGE_CHARS", 262_144)?,
            session_cookie: env_or("GATEWAY_SESSION_COOKIE", DEFAULT_SESSION_COOKIE),
            csrf_cookie: env_or("GATEWAY_CSRF_COOKIE", DEFAULT_CSRF_COOKIE),
            login_cookie: env_or("GATEWAY_LOGIN_COOKIE", DEFAULT_LOGIN_COOKIE),
        };

        let ui_url = Url::parse(&config.ui_public_url)
            .context("UI_PUBLIC_URL must be a valid URL")?;
        let callback_url = Url::parse(&config.oidc_callback_url)
            .context("OIDC_CALLBACK_URL must be a valid URL")?;
        if ui_url.scheme() != callback_url.scheme()
            || ui_url.host_str() != callback_url.host_str()
            || ui_url.port_or_known_default() != callback_url.port_or_known_default()
        {
            anyhow::bail!(
                "OIDC_CALLBACK_URL must use the same origin as UI_PUBLIC_URL because the UI proxies the BFF callback"
            );
        }
        if callback_url.path() != OIDC_CALLBACK_PATH {
            anyhow::bail!(
                "OIDC_CALLBACK_URL path must be exactly /api/gateway/auth/callback"
            );
        }
        Url::parse(&config.keycloak_public_url).context("KEYCLOAK_PUBLIC_URL must be a valid URL")?;
        Url::parse(&config.keycloak_internal_url).context("KEYCLOAK_INTERNAL_URL must be a valid URL")?;
        Url::parse(&config.agent_workflow_url).context("AGENT_WORKFLOW_URL must be a valid URL")?;
        for (name, value) in [
            ("GATEWAY_SESSION_COOKIE", config.session_cookie.as_str()),
            ("GATEWAY_CSRF_COOKIE", config.csrf_cookie.as_str()),
            ("GATEWAY_LOGIN_COOKIE", config.login_cookie.as_str()),
        ] {
            if !valid_cookie_name(value) {
                anyhow::bail!("{name} must be a valid cookie name");
            }
        }
        Ok(config)
    }

    fn callback_url(&self) -> &str {
        &self.oidc_callback_url
    }

    fn callback_cookie_path(&self) -> &'static str {
        OIDC_CALLBACK_PATH
    }

    fn realm_endpoint(&self, base: &str, suffix: &str) -> String {
        format!(
            "{}/realms/{}/{}",
            base.trim_end_matches('/'),
            self.keycloak_realm,
            suffix.trim_start_matches('/')
        )
    }

    fn cookie_suffix(&self) -> &'static str {
        if self.cookie_secure {
            "; Secure"
        } else {
            ""
        }
    }
}

fn env_or(name: &str, default: &str) -> String {
    std::env::var(name)
        .ok()
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| default.to_owned())
}

fn required_env(name: &str) -> Result<String> {
    let value = std::env::var(name).with_context(|| format!("{name} must be configured"))?;
    let value = value.trim();
    if value.is_empty() {
        anyhow::bail!("{name} must not be empty");
    }
    Ok(value.to_owned())
}

fn valid_cookie_name(name: &str) -> bool {
    !name.is_empty()
        && name.bytes().all(|byte| {
            byte.is_ascii_alphanumeric()
                || matches!(byte, b'!' | b'#' | b'$' | b'%' | b'&' | b'\'' | b'*' | b'+' | b'-' | b'.' | b'^' | b'_' | b'`' | b'|' | b'~')
        })
}

fn env_bool(name: &str, default: bool) -> bool {
    std::env::var(name)
        .ok()
        .map(|value| matches!(value.to_ascii_lowercase().as_str(), "1" | "true" | "yes" | "on"))
        .unwrap_or(default)
}

fn env_u64(name: &str, default: u64) -> Result<u64> {
    match std::env::var(name) {
        Ok(value) => value
            .parse::<u64>()
            .with_context(|| format!("{name} must be an unsigned integer")),
        Err(_) => Ok(default),
    }
}

fn env_usize(name: &str, default: usize) -> Result<usize> {
    let value = match std::env::var(name) {
        Ok(value) => value
            .parse::<usize>()
            .with_context(|| format!("{name} must be a positive integer"))?,
        Err(_) => default,
    };
    if value == 0 {
        anyhow::bail!("{name} must be greater than zero");
    }
    Ok(value)
}

fn random_token() -> String {
    format!(
        "{}{}{}",
        Uuid::new_v4().simple(),
        Uuid::new_v4().simple(),
        Uuid::new_v4().simple()
    )
}

fn pkce_challenge(verifier: &str) -> String {
    URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()))
}

fn constant_time_eq(left: &str, right: &str) -> bool {
    let left = left.as_bytes();
    let right = right.as_bytes();
    if left.len() != right.len() {
        return false;
    }
    left.iter()
        .zip(right.iter())
        .fold(0_u8, |diff, (a, b)| diff | (a ^ b))
        == 0
}

fn cookie_value(headers: &HeaderMap, name: &str) -> Option<String> {
    let cookie = headers.get(header::COOKIE)?.to_str().ok()?;
    cookie.split(';').find_map(|part| {
        let (key, value) = part.trim().split_once('=')?;
        (key == name).then(|| value.to_owned())
    })
}

fn session_cookie(config: &GatewayConfig, value: &str) -> HeaderValue {
    HeaderValue::from_str(&format!(
        "{}={}; Path={}; Max-Age={}; HttpOnly; SameSite=Lax{}",
        config.session_cookie,
        value,
        BROWSER_COOKIE_PATH,
        config.session_ttl.as_secs(),
        config.cookie_suffix(),
    ))
    .expect("opaque session cookie must be a valid header")
}

fn csrf_cookie(config: &GatewayConfig, value: &str) -> HeaderValue {
    HeaderValue::from_str(&format!(
        "{}={}; Path={}; Max-Age={}; SameSite=Strict{}",
        config.csrf_cookie,
        value,
        BROWSER_COOKIE_PATH,
        config.session_ttl.as_secs(),
        config.cookie_suffix(),
    ))
    .expect("opaque CSRF cookie must be a valid header")
}

fn login_cookie(config: &GatewayConfig, value: &str) -> HeaderValue {
    HeaderValue::from_str(&format!(
        "{}={}; Path={}; Max-Age=600; HttpOnly; SameSite=Lax{}",
        config.login_cookie,
        value,
        config.callback_cookie_path(),
        config.cookie_suffix(),
    ))
    .expect("opaque login cookie must be a valid header")
}

fn clear_cookie(config: &GatewayConfig, name: &str, path: &str, http_only: bool) -> HeaderValue {
    HeaderValue::from_str(&format!(
        "{}=; Path={}; Max-Age=0; {}SameSite=Lax{}",
        name,
        path,
        if http_only { "HttpOnly; " } else { "" },
        config.cookie_suffix(),
    ))
    .expect("cookie clearing header must be valid")
}

fn safe_return_to(candidate: Option<String>, configured_ui: &str) -> String {
    let Some(candidate) = candidate else {
        return configured_ui.to_owned();
    };
    let Ok(base) = Url::parse(configured_ui) else {
        return configured_ui.to_owned();
    };
    let Ok(url) = Url::parse(&candidate) else {
        return configured_ui.to_owned();
    };
    let same_origin = base.scheme() == url.scheme()
        && base.host_str() == url.host_str()
        && base.port_or_known_default() == url.port_or_known_default();
    if same_origin {
        candidate
    } else {
        configured_ui.to_owned()
    }
}

fn validate_chat_request(
    config: &GatewayConfig,
    request: &ChatProxyRequest,
) -> Result<(), GatewayError> {
    if request.messages.is_empty() {
        return Err(GatewayError::bad_request("at least one chat message is required"));
    }
    if request.messages.len() > config.max_messages {
        return Err(GatewayError::bad_request(format!(
            "too many chat messages; maximum is {}",
            config.max_messages
        )));
    }

    let mut total_chars = 0_usize;
    for message in &request.messages {
        if !matches!(message.role.as_str(), "user" | "assistant") {
            return Err(GatewayError::bad_request(
                "only user and assistant chat roles are accepted",
            ));
        }
        if message.content.trim().is_empty() {
            return Err(GatewayError::bad_request("chat messages must not be empty"));
        }
        let chars = message.content.chars().count();
        if chars > config.max_message_chars {
            return Err(GatewayError::bad_request(format!(
                "chat message is too large; maximum is {} characters",
                config.max_message_chars
            )));
        }
        total_chars = total_chars.saturating_add(chars);
        if total_chars > config.max_total_message_chars {
            return Err(GatewayError::bad_request(format!(
                "chat history is too large; maximum is {} characters",
                config.max_total_message_chars
            )));
        }
    }

    if request.messages.last().map(|message| message.role.as_str()) != Some("user") {
        return Err(GatewayError::bad_request(
            "the final chat message must have the user role",
        ));
    }
    Ok(())
}

async fn health() -> impl IntoResponse {
    Json(json!({ "status": "ok" }))
}

async fn ready(State(state): State<AppState>) -> Result<impl IntoResponse, GatewayError> {
    let discovery = state.config.realm_endpoint(
        &state.config.keycloak_internal_url,
        ".well-known/openid-configuration",
    );
    let keycloak_ok = state
        .client
        .get(discovery)
        .send()
        .await
        .map(|response| response.status().is_success())
        .unwrap_or(false);
    let agent_health = agent_health_url(&state.config.agent_workflow_url);
    let agent_ok = state
        .client
        .get(agent_health)
        .send()
        .await
        .map(|response| response.status().is_success())
        .unwrap_or(false);

    if keycloak_ok && agent_ok {
        Ok(Json(json!({ "status": "ready" })))
    } else {
        Err(GatewayError {
            status: StatusCode::SERVICE_UNAVAILABLE,
            message: format!("dependencies unavailable: keycloak={keycloak_ok}, agent={agent_ok}"),
        })
    }
}

fn agent_health_url(workflow_url: &str) -> String {
    Url::parse(workflow_url)
        .map(|mut url| {
            url.set_path("/health");
            url.set_query(None);
            url.to_string()
        })
        .unwrap_or_else(|_| "http://agent:8000/health".to_owned())
}

async fn login(
    State(state): State<AppState>,
    Query(query): Query<LoginQuery>,
) -> Result<Response, GatewayError> {
    let login_id = random_token();
    let oauth_state = random_token();
    let nonce = random_token();
    let verifier = random_token();
    let return_to = safe_return_to(query.return_to, &state.config.ui_public_url);

    {
        let mut pending = state.pending.write().await;
        pending.retain(|_, login| login.created_at.elapsed() < Duration::from_secs(600));
        if pending.len() >= state.config.max_pending_logins {
            return Err(GatewayError::too_many_requests(
                "too many pending login attempts; try again shortly",
            ));
        }
        pending.insert(
            login_id.clone(),
            PendingLogin {
                state: oauth_state.clone(),
                nonce: nonce.clone(),
                verifier: verifier.clone(),
                return_to,
                created_at: Instant::now(),
            },
        );
    }

    let mut authorization_url = Url::parse(&state.config.realm_endpoint(
        &state.config.keycloak_public_url,
        "protocol/openid-connect/auth",
    ))
    .map_err(|error| GatewayError::bad_gateway(format!("invalid authorization URL: {error}")))?;
    authorization_url
        .query_pairs_mut()
        .append_pair("client_id", &state.config.oidc_client_id)
        .append_pair("response_type", "code")
        .append_pair("redirect_uri", state.config.callback_url())
        .append_pair("scope", "openid profile email roles")
        .append_pair("state", &oauth_state)
        .append_pair("nonce", &nonce)
        .append_pair("code_challenge", &pkce_challenge(&verifier))
        .append_pair("code_challenge_method", "S256");

    let mut response = Redirect::temporary(authorization_url.as_str()).into_response();
    response
        .headers_mut()
        .append(header::SET_COOKIE, login_cookie(&state.config, &login_id));
    Ok(response)
}

async fn callback(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<CallbackQuery>,
) -> Result<Response, GatewayError> {
    if let Some(error) = query.error {
        return Err(GatewayError::unauthorized(format!(
            "Keycloak rejected authentication: {}",
            query.error_description.unwrap_or(error)
        )));
    }

    let login_id = cookie_value(&headers, &state.config.login_cookie)
        .ok_or_else(|| GatewayError::bad_request("missing login transaction cookie"))?;
    let pending = state
        .pending
        .write()
        .await
        .remove(&login_id)
        .ok_or_else(|| GatewayError::bad_request("login transaction expired or was already used"))?;

    if pending.created_at.elapsed() > Duration::from_secs(600) {
        return Err(GatewayError::bad_request("login transaction expired"));
    }
    let returned_state = query
        .state
        .ok_or_else(|| GatewayError::bad_request("missing OAuth state"))?;
    if !constant_time_eq(&returned_state, &pending.state) {
        return Err(GatewayError::bad_request("invalid OAuth state"));
    }
    let code = query
        .code
        .ok_or_else(|| GatewayError::bad_request("missing authorization code"))?;

    let token = exchange_code(&state, &code, &pending.verifier).await?;
    let id_token = token
        .id_token
        .as_deref()
        .ok_or_else(|| GatewayError::unauthorized("Keycloak did not return an ID token"))?;
    validate_id_token(&state, id_token, &pending.nonce).await?;
    let user = fetch_user_info(&state, &token.access_token).await?;
    let identity = UserIdentity {
        id: user.sub,
        username: user
            .preferred_username
            .unwrap_or_else(|| "unknown".to_owned()),
        email: user.email,
        name: user.name,
        roles: user.realm_access.map(|access| access.roles).unwrap_or_default(),
    };

    let session_id = random_token();
    let csrf_token = random_token();
    let now = Instant::now();
    {
        let mut sessions = state.sessions.write().await;
        sessions.retain(|_, session| session.session_expires_at > now);
        if sessions.len() >= state.config.max_sessions {
            return Err(GatewayError::too_many_requests(
                "the gateway session capacity has been reached",
            ));
        }
        sessions.insert(
            session_id.clone(),
            Session {
                user: identity,
                refresh_token: token.refresh_token,
                id_token: token.id_token,
                csrf_token: csrf_token.clone(),
                access_expires_at: now + Duration::from_secs(token.expires_in),
                session_expires_at: now + state.config.session_ttl,
            },
        );
    }

    let mut response = Redirect::to(&pending.return_to).into_response();
    response
        .headers_mut()
        .append(header::SET_COOKIE, session_cookie(&state.config, &session_id));
    response
        .headers_mut()
        .append(header::SET_COOKIE, csrf_cookie(&state.config, &csrf_token));
    response.headers_mut().append(
        header::SET_COOKIE,
        clear_cookie(
            &state.config,
            &state.config.login_cookie,
            state.config.callback_cookie_path(),
            true,
        ),
    );
    Ok(response)
}

async fn exchange_code(
    state: &AppState,
    code: &str,
    verifier: &str,
) -> Result<TokenResponse, GatewayError> {
    let token_url = state.config.realm_endpoint(
        &state.config.keycloak_internal_url,
        "protocol/openid-connect/token",
    );
    let callback_url = state.config.callback_url();
    let response = state
        .client
        .post(token_url)
        .form(&[
            ("grant_type", "authorization_code"),
            ("client_id", state.config.oidc_client_id.as_str()),
            ("client_secret", state.config.oidc_client_secret.as_str()),
            ("code", code),
            ("redirect_uri", callback_url),
            ("code_verifier", verifier),
        ])
        .send()
        .await
        .map_err(|error| GatewayError::bad_gateway(format!("token exchange failed: {error}")))?;

    let status = response.status();
    if !status.is_success() {
        let details = response.text().await.unwrap_or_default();
        return Err(GatewayError::unauthorized(format!(
            "token exchange rejected ({status}): {details}"
        )));
    }
    response
        .json::<TokenResponse>()
        .await
        .map_err(|error| GatewayError::bad_gateway(format!("invalid token response: {error}")))
}


async fn validate_id_token(
    state: &AppState,
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
    let jwks_url = state.config.realm_endpoint(
        &state.config.keycloak_internal_url,
        "protocol/openid-connect/certs",
    );
    let response = state
        .client
        .get(jwks_url)
        .send()
        .await
        .map_err(|error| GatewayError::bad_gateway(format!("JWKS request failed: {error}")))?;
    if !response.status().is_success() {
        return Err(GatewayError::bad_gateway("Keycloak JWKS endpoint rejected the request"));
    }
    let jwks = response
        .json::<JwkSet>()
        .await
        .map_err(|error| GatewayError::bad_gateway(format!("invalid Keycloak JWKS: {error}")))?;
    let jwk = jwks
        .find(kid)
        .ok_or_else(|| GatewayError::unauthorized("ID token signing key was not found"))?;
    let decoding_key = DecodingKey::from_jwk(jwk)
        .map_err(|_| GatewayError::unauthorized("unsupported ID token signing key"))?;

    let expected_issuer = state
        .config
        .realm_endpoint(&state.config.keycloak_public_url, "")
        .trim_end_matches('/')
        .to_owned();
    let mut validation = Validation::new(Algorithm::RS256);
    validation.set_audience(&[state.config.oidc_client_id.as_str()]);
    validation.set_issuer(&[expected_issuer.as_str()]);
    validation.set_required_spec_claims(&["exp", "iss", "aud", "sub"]);
    validation.validate_exp = true;
    let token = decode::<IdTokenClaims>(id_token, &decoding_key, &validation)
        .map_err(|error| GatewayError::unauthorized(format!("ID token validation failed: {error}")))?;

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

async fn fetch_user_info(state: &AppState, access_token: &str) -> Result<UserInfo, GatewayError> {
    let userinfo_url = state.config.realm_endpoint(
        &state.config.keycloak_internal_url,
        "protocol/openid-connect/userinfo",
    );
    let response = state
        .client
        .get(userinfo_url)
        .bearer_auth(access_token)
        .send()
        .await
        .map_err(|error| GatewayError::bad_gateway(format!("userinfo request failed: {error}")))?;
    if !response.status().is_success() {
        return Err(GatewayError::unauthorized("Keycloak rejected the access token"));
    }
    response
        .json::<UserInfo>()
        .await
        .map_err(|error| GatewayError::bad_gateway(format!("invalid userinfo response: {error}")))
}

async fn refresh_session(
    state: &AppState,
    session_id: &str,
    session: Session,
) -> Result<Session, GatewayError> {
    if session.access_expires_at > Instant::now() + Duration::from_secs(30) {
        return Ok(session);
    }
    let refresh_token = session
        .refresh_token
        .clone()
        .ok_or_else(|| GatewayError::unauthorized("session expired"))?;
    let token_url = state.config.realm_endpoint(
        &state.config.keycloak_internal_url,
        "protocol/openid-connect/token",
    );
    let response = state
        .client
        .post(token_url)
        .form(&[
            ("grant_type", "refresh_token"),
            ("client_id", state.config.oidc_client_id.as_str()),
            ("client_secret", state.config.oidc_client_secret.as_str()),
            ("refresh_token", refresh_token.as_str()),
        ])
        .send()
        .await
        .map_err(|error| GatewayError::bad_gateway(format!("token refresh failed: {error}")))?;
    if !response.status().is_success() {
        state.sessions.write().await.remove(session_id);
        return Err(GatewayError::unauthorized("session could not be refreshed"));
    }
    let token = response
        .json::<TokenResponse>()
        .await
        .map_err(|error| GatewayError::bad_gateway(format!("invalid refresh response: {error}")))?;
    let mut refreshed = session;
    refreshed.access_expires_at = Instant::now() + Duration::from_secs(token.expires_in);
    if token.refresh_token.is_some() {
        refreshed.refresh_token = token.refresh_token;
    }
    if token.id_token.is_some() {
        refreshed.id_token = token.id_token;
    }
    state
        .sessions
        .write()
        .await
        .insert(session_id.to_owned(), refreshed.clone());
    Ok(refreshed)
}

async fn authenticated_session(
    state: &AppState,
    headers: &HeaderMap,
) -> Result<(String, Session), GatewayError> {
    let session_id = cookie_value(headers, &state.config.session_cookie)
        .ok_or_else(|| GatewayError::unauthorized("authentication required"))?;
    let session = state
        .sessions
        .read()
        .await
        .get(&session_id)
        .cloned()
        .ok_or_else(|| GatewayError::unauthorized("session not found"))?;
    if session.session_expires_at <= Instant::now() {
        state.sessions.write().await.remove(&session_id);
        return Err(GatewayError::unauthorized("session expired"));
    }
    let session = refresh_session(state, &session_id, session).await?;
    Ok((session_id, session))
}

fn verify_csrf(config: &GatewayConfig, headers: &HeaderMap, session: &Session) -> Result<(), GatewayError> {
    let cookie = cookie_value(headers, &config.csrf_cookie)
        .ok_or_else(|| GatewayError::forbidden("missing CSRF cookie"))?;
    let supplied = headers
        .get("x-csrf-token")
        .and_then(|value| value.to_str().ok())
        .ok_or_else(|| GatewayError::forbidden("missing CSRF header"))?;
    if !constant_time_eq(&cookie, supplied) || !constant_time_eq(supplied, &session.csrf_token) {
        return Err(GatewayError::forbidden("invalid CSRF token"));
    }
    Ok(())
}

async fn auth_session(State(state): State<AppState>, headers: HeaderMap) -> Response {
    match authenticated_session(&state, &headers).await {
        Ok((_session_id, session)) => Json(json!({
            "authenticated": true,
            "user": session.user,
        }))
        .into_response(),
        Err(_) => Json(json!({ "authenticated": false })).into_response(),
    }
}

async fn logout(State(state): State<AppState>, headers: HeaderMap) -> Result<Response, GatewayError> {
    let maybe_session_id = cookie_value(&headers, &state.config.session_cookie);
    let mut id_token = None;
    if let Some(session_id) = maybe_session_id {
        if let Some(session) = state.sessions.read().await.get(&session_id).cloned() {
            verify_csrf(&state.config, &headers, &session)?;
            id_token = session.id_token.clone();
        }
        state.sessions.write().await.remove(&session_id);
    }

    let mut logout_url = Url::parse(&state.config.realm_endpoint(
        &state.config.keycloak_public_url,
        "protocol/openid-connect/logout",
    ))
    .map_err(|error| GatewayError::bad_gateway(format!("invalid logout URL: {error}")))?;
    logout_url
        .query_pairs_mut()
        .append_pair("client_id", &state.config.oidc_client_id)
        .append_pair("post_logout_redirect_uri", &state.config.ui_public_url);
    if let Some(id_token) = id_token {
        logout_url
            .query_pairs_mut()
            .append_pair("id_token_hint", &id_token);
    }

    let mut response = Json(json!({ "logout_url": logout_url.as_str() })).into_response();
    response.headers_mut().append(
        header::SET_COOKIE,
        clear_cookie(
            &state.config,
            &state.config.session_cookie,
            BROWSER_COOKIE_PATH,
            true,
        ),
    );
    response.headers_mut().append(
        header::SET_COOKIE,
        clear_cookie(
            &state.config,
            &state.config.csrf_cookie,
            BROWSER_COOKIE_PATH,
            false,
        ),
    );
    Ok(response)
}

async fn chat(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Response, GatewayError> {
    let (_session_id, session) = authenticated_session(&state, &headers).await?;
    verify_csrf(&state.config, &headers, &session)?;

    let chat_request = serde_json::from_slice::<ChatProxyRequest>(&body)
        .map_err(|error| GatewayError::bad_request(format!("invalid chat request: {error}")))?;
    validate_chat_request(&state.config, &chat_request)?;
    let sanitized_body = serde_json::to_vec(&chat_request)
        .map_err(|error| GatewayError::bad_request(format!("could not serialize chat request: {error}")))?;

    let mut workflow_url = Url::parse(&state.config.agent_workflow_url)
        .map_err(|error| GatewayError::bad_gateway(format!("invalid agent URL: {error}")))?;
    workflow_url
        .query_pairs_mut()
        .append_pair("filter_steps", &state.config.agent_filter_steps);

    let request_id = Uuid::new_v4().to_string();
    let mut request = state
        .client
        .post(workflow_url)
        .header(header::AUTHORIZATION, format!("Bearer {}", state.config.agent_api_key))
        .header(header::CONTENT_TYPE, "application/json")
        .header(header::ACCEPT, "text/event-stream")
        .header("x-request-id", &request_id)
        .body(sanitized_body);

    request = trusted_header(request, "x-authenticated-user-id", &session.user.id);
    request = trusted_header(
        request,
        "x-authenticated-username",
        &session.user.username,
    );
    if let Some(email) = &session.user.email {
        request = trusted_header(request, "x-authenticated-email", email);
    }
    request = trusted_header(
        request,
        "x-authenticated-roles",
        &session.user.roles.join(","),
    );

    let upstream = request
        .send()
        .await
        .map_err(|error| GatewayError::bad_gateway(format!("agent request failed: {error}")))?;
    let status = upstream.status();

    if !status.is_success() {
        let details = upstream.text().await.unwrap_or_default();
        let mut response = (
            status,
            Json(json!({
                "error": "agent request rejected",
                "details": details,
                "request_id": request_id,
            })),
        )
            .into_response();
        response.headers_mut().insert(
            HeaderName::from_static("x-request-id"),
            HeaderValue::from_str(&request_id).expect("UUID must be a valid header"),
        );
        return Ok(response);
    }

    let content_type = upstream
        .headers()
        .get(header::CONTENT_TYPE)
        .cloned()
        .unwrap_or_else(|| HeaderValue::from_static("text/event-stream"));
    let stream = upstream
        .bytes_stream()
        .map_err(std::io::Error::other);
    let mut response = Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, content_type)
        .header(header::CACHE_CONTROL, "no-cache, no-store")
        .header("x-accel-buffering", "no")
        .header("x-request-id", request_id)
        .body(Body::from_stream(stream))
        .map_err(|error| GatewayError::bad_gateway(format!("could not create stream response: {error}")))?;
    response.headers_mut().remove(header::CONTENT_LENGTH);
    Ok(response)
}

fn trusted_header(
    request: reqwest::RequestBuilder,
    name: &'static str,
    value: &str,
) -> reqwest::RequestBuilder {
    match HeaderValue::from_str(value) {
        Ok(value) => request.header(HeaderName::from_static(name), value),
        Err(_) => request,
    }
}

async fn security_headers(request: Request, next: Next) -> Response {
    let mut response = next.run(request).await;
    let headers = response.headers_mut();
    headers.insert(
        HeaderName::from_static("x-content-type-options"),
        HeaderValue::from_static("nosniff"),
    );
    headers.insert(
        HeaderName::from_static("referrer-policy"),
        HeaderValue::from_static("no-referrer"),
    );
    headers.insert(
        HeaderName::from_static("x-frame-options"),
        HeaderValue::from_static("DENY"),
    );
    headers.insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static("no-store"),
    );
    response
}

async fn cleanup_expired(state: AppState) {
    let mut interval = tokio::time::interval(Duration::from_secs(60));
    loop {
        interval.tick().await;
        let now = Instant::now();
        state
            .pending
            .write()
            .await
            .retain(|_, pending| pending.created_at.elapsed() < Duration::from_secs(600));
        state
            .sessions
            .write()
            .await
            .retain(|_, session| session.session_expires_at > now);
    }
}

#[tokio::main]
async fn main() {
    if let Err(error) = run().await {
        eprintln!("alerts authentication gateway failed: {error:#}");
        std::process::exit(1);
    }
}

async fn run() -> Result<()> {
    tracing_subscriber::registry()
        .with(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "alerts_auth_gateway=info,tower_http=info".into()),
        )
        .with(tracing_subscriber::fmt::layer())
        .init();

    let config = Arc::new(GatewayConfig::from_env()?);
    let client = Client::builder()
        .connect_timeout(Duration::from_secs(10))
        .pool_idle_timeout(Duration::from_secs(90))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .context("failed to build HTTP client")?;
    let state = AppState {
        config: config.clone(),
        client,
        sessions: Arc::new(RwLock::new(HashMap::new())),
        pending: Arc::new(RwLock::new(HashMap::new())),
    };

    tokio::spawn(cleanup_expired(state.clone()));

    let app = Router::new()
        .route("/health", get(health))
        .route("/ready", get(ready))
        .route("/auth/login", get(login))
        .route("/auth/callback", get(callback))
        .route("/auth/session", get(auth_session))
        .route("/auth/logout", post(logout))
        .route("/api/chat", post(chat))
        .with_state(state)
        .layer(middleware::from_fn(security_headers));

    let address: SocketAddr = config
        .bind_address
        .parse()
        .context("GATEWAY_BIND_ADDRESS is invalid")?;
    let listener = tokio::net::TcpListener::bind(address)
        .await
        .context("failed to bind authentication gateway")?;
    info!(%address, "alerts authentication gateway listening");
    axum::serve(listener, app)
        .with_graceful_shutdown(async {
            if let Err(error) = tokio::signal::ctrl_c().await {
                warn!(%error, "could not install shutdown signal");
            }
        })
        .await
        .context("authentication gateway server failed")?;
    Ok(())
}
