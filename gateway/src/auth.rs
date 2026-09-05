//! The authorization-code flow, and turning its result into a browser session.

use std::{
    collections::HashMap,
    time::{Duration, Instant},
};

use axum::{
    extract::{Query, State},
    http::{header, HeaderMap},
    response::{IntoResponse, Redirect, Response},
    Json,
};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use serde::Deserialize;
use serde_json::json;
use sha2::{Digest, Sha256};
use tokio::sync::RwLock;
use url::Url;
use uuid::Uuid;

use crate::config::{same_origin, BROWSER_COOKIE_PATH, PENDING_LOGIN_TTL};
use crate::cookies::{clear_cookie, cookie_value, csrf_cookie, login_cookie, session_cookie};
use crate::error::GatewayError;
use crate::oidc::RefreshOutcome;
use crate::session::{
    constant_time_eq, session_id_from, Generation, Session, WriteBack,
};
use crate::state::AppState;

#[derive(Debug, Deserialize)]
pub struct LoginQuery {
    return_to: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct CallbackQuery {
    code: Option<String>,
    state: Option<String>,
    error: Option<String>,
    error_description: Option<String>,
}

#[derive(Clone)]
pub struct PendingLogin {
    pub state: String,
    pub nonce: String,
    pub verifier: String,
    pub return_to: String,
    pub created_at: Instant,
}

/// Half-finished logins, from the redirect to Keycloak until the callback.
pub struct PendingLoginStore {
    logins: RwLock<HashMap<String, PendingLogin>>,
    capacity: usize,
}

impl PendingLoginStore {
    pub fn new(capacity: usize) -> Self {
        Self { logins: RwLock::new(HashMap::new()), capacity }
    }

    /// Record a pending login, evicting to make room rather than refusing.
    ///
    /// Refusing the *newest* login at capacity handed an unauthenticated attacker
    /// a complete login outage: `/auth/login` needs no credentials, each call held
    /// a slot for ten minutes, and a thousand of them locked every user out for
    /// that long. Evicting the oldest instead means a flood can only displace
    /// logins still in the few seconds between redirect and callback, and the flood
    /// has to be sustained to do even that.
    pub async fn insert(&self, login_id: String, login: PendingLogin) {
        let mut logins = self.logins.write().await;
        logins.retain(|_, pending| pending.created_at.elapsed() < PENDING_LOGIN_TTL);
        while logins.len() >= self.capacity {
            let Some(oldest) = logins
                .iter()
                .min_by_key(|(_, pending)| pending.created_at)
                .map(|(id, _)| id.clone())
            else {
                break;
            };
            logins.remove(&oldest);
        }
        logins.insert(login_id, login);
    }

    /// Consume a pending login. One-time by construction: it is removed before it
    /// is validated, so a replayed callback finds nothing.
    pub async fn take(&self, login_id: &str) -> Option<PendingLogin> {
        self.logins.write().await.remove(login_id)
    }

    pub async fn drop_expired(&self) {
        self.logins
            .write()
            .await
            .retain(|_, pending| pending.created_at.elapsed() < PENDING_LOGIN_TTL);
    }

    #[cfg(test)]
    pub async fn len(&self) -> usize {
        self.logins.read().await.len()
    }
}

pub fn random_token() -> String {
    format!(
        "{}{}{}",
        Uuid::new_v4().simple(),
        Uuid::new_v4().simple(),
        Uuid::new_v4().simple()
    )
}

pub fn pkce_challenge(verifier: &str) -> String {
    URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()))
}

/// Resolve a post-login destination that can only ever be the configured UI.
///
/// Absolute URLs are accepted when same-origin; relative ones are resolved
/// against the UI origin, which previously fell through to the UI root because
/// `Url::parse` rejects them outright. Anything else — cross-origin, scheme
/// relative, unparseable — falls back to the configured UI.
pub fn safe_return_to(candidate: Option<&str>, configured_ui: &str) -> String {
    let Ok(base) = Url::parse(configured_ui) else {
        return configured_ui.to_owned();
    };
    let Some(candidate) = candidate.map(str::trim).filter(|value| !value.is_empty()) else {
        return configured_ui.to_owned();
    };
    match base.join(candidate) {
        Ok(url) if same_origin(&base, &url) => url.to_string(),
        _ => configured_ui.to_owned(),
    }
}

pub async fn login(
    State(state): State<AppState>,
    Query(query): Query<LoginQuery>,
) -> Result<Response, GatewayError> {
    let login_id = random_token();
    let oauth_state = random_token();
    let nonce = random_token();
    let verifier = random_token();
    let return_to = safe_return_to(query.return_to.as_deref(), &state.config.ui_public_url);

    state
        .pending
        .insert(
            login_id.clone(),
            PendingLogin {
                state: oauth_state.clone(),
                nonce: nonce.clone(),
                verifier: verifier.clone(),
                return_to,
                created_at: Instant::now(),
            },
        )
        .await;

    let authorization_url =
        state
            .keycloak
            .authorization_url(&oauth_state, &nonce, &pkce_challenge(&verifier))?;

    let mut response = Redirect::temporary(authorization_url.as_str()).into_response();
    response
        .headers_mut()
        .append(header::SET_COOKIE, login_cookie(&state.config, &login_id));
    Ok(response)
}

pub async fn callback(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<CallbackQuery>,
) -> Result<Response, GatewayError> {
    if let Some(error) = query.error {
        tracing::info!(
            error,
            description = query.error_description,
            "keycloak rejected authentication"
        );
        return Err(GatewayError::unauthorized("authentication was not completed"));
    }

    let login_id = cookie_value(&headers, &state.config.login_cookie)
        .ok_or_else(|| GatewayError::bad_request("missing login transaction cookie"))?;
    let pending = state
        .pending
        .take(&login_id)
        .await
        .ok_or_else(|| GatewayError::bad_request("login transaction expired or was already used"))?;

    if pending.created_at.elapsed() > PENDING_LOGIN_TTL {
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

    let token = state.keycloak.exchange_code(&code, &pending.verifier).await?;
    let id_token = token
        .id_token
        .as_deref()
        .ok_or_else(|| GatewayError::unauthorized("Keycloak did not return an ID token"))?;
    state.keycloak.validate_id_token(id_token, &pending.nonce).await?;
    let user = state.keycloak.user_info(&token.access_token).await?;

    let session_id = random_token();
    let csrf_token = random_token();
    let session = Session::new(
        user,
        csrf_token.clone(),
        token.refresh_token,
        token.id_token,
        Duration::from_secs(token.expires_in),
        state.config.session_ttl,
        state.config.max_streams_per_session,
    );
    state.sessions.insert(session_id.clone(), session).await?;

    let mut response = Redirect::to(&pending.return_to).into_response();
    let response_headers = response.headers_mut();
    response_headers.append(header::SET_COOKIE, session_cookie(&state.config, &session_id));
    response_headers.append(header::SET_COOKIE, csrf_cookie(&state.config, &csrf_token));
    response_headers.append(
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

/// Resolve the caller's session, refreshing it against Keycloak when due.
pub async fn authenticated_session(
    state: &AppState,
    headers: &HeaderMap,
) -> Result<(String, Session), GatewayError> {
    let session_id = session_id_from(&state.config, headers)
        .ok_or_else(|| GatewayError::unauthorized("authentication required"))?;
    let (session, generation) = state
        .sessions
        .get(&session_id)
        .await
        .ok_or_else(|| GatewayError::unauthorized("session not found"))?;

    let now = Instant::now();
    if session.is_expired(now) {
        state.sessions.remove_if_current(&session_id, generation).await;
        return Err(GatewayError::unauthorized("session expired"));
    }
    if !session.needs_refresh(now) {
        return Ok((session_id, session));
    }
    let session = refresh(state, &session_id, session, generation).await?;
    Ok((session_id, session))
}

/// Renew a session's tokens and identity.
///
/// Every exit from this function respects one rule: a write-back may only touch
/// the session it was derived from. See [`crate::session`] for why.
async fn refresh(
    state: &AppState,
    session_id: &str,
    session: Session,
    generation: Generation,
) -> Result<Session, GatewayError> {
    let Some(refresh_token) = session.refresh_token.clone() else {
        state.sessions.remove_if_current(session_id, generation).await;
        return Err(GatewayError::unauthorized("session expired"));
    };

    let token = match state.keycloak.refresh(&refresh_token).await? {
        RefreshOutcome::Renewed(token) => token,
        RefreshOutcome::Rejected => {
            return match state.sessions.remove_if_current(session_id, generation).await {
                WriteBack::Applied | WriteBack::Gone => {
                    Err(GatewayError::unauthorized("session could not be refreshed"))
                }
                // Another request refreshed first and Keycloak rotated our token
                // out from under us. Its session is live; ours was merely stale,
                // and revoking on this would log the user out mid-approval.
                WriteBack::Superseded => current_session(state, session_id).await,
            };
        }
    };

    // Roles are re-read on every refresh. They used to be captured once at login
    // and then forwarded to the agent unchanged for the whole session TTL, so a
    // role revoked in Keycloak stayed in effect here for up to eight hours.
    let user = state.keycloak.user_info(&token.access_token).await?;
    if user.id != session.user.id {
        tracing::error!("refreshed session resolved to a different subject");
        state.sessions.remove(session_id).await;
        return Err(GatewayError::unauthorized("session identity changed"));
    }

    let mut refreshed = session;
    refreshed.user = user;
    refreshed.access_expires_at = Instant::now() + Duration::from_secs(token.expires_in);
    if token.refresh_token.is_some() {
        refreshed.refresh_token = token.refresh_token;
    }
    if token.id_token.is_some() {
        refreshed.id_token = token.id_token;
    }

    match state
        .sessions
        .replace_if_current(session_id, generation, refreshed.clone())
        .await
    {
        WriteBack::Applied => Ok(refreshed),
        WriteBack::Superseded => current_session(state, session_id).await,
        // Logged out while we were talking to Keycloak. Re-inserting here is the
        // resurrection that made logout unreliable; the session stays revoked.
        WriteBack::Gone => Err(GatewayError::unauthorized("session ended")),
    }
}

async fn current_session(state: &AppState, session_id: &str) -> Result<Session, GatewayError> {
    state
        .sessions
        .get(session_id)
        .await
        .map(|(session, _)| session)
        .ok_or_else(|| GatewayError::unauthorized("session ended"))
}

pub async fn auth_session(State(state): State<AppState>, headers: HeaderMap) -> Response {
    match authenticated_session(&state, &headers).await {
        Ok((_, session)) => {
            Json(json!({ "authenticated": true, "user": session.user })).into_response()
        }
        // A dependency being down is not the same as being logged out, and telling
        // the UI otherwise sends the user through a login that cannot succeed.
        Err(error) if error.status.is_server_error() => error.into_response(),
        Err(_) => Json(json!({ "authenticated": false })).into_response(),
    }
}

pub async fn logout(
    State(state): State<AppState>,
    headers: HeaderMap,
) -> Result<Response, GatewayError> {
    let mut id_token = None;
    if let Some(session_id) = session_id_from(&state.config, &headers) {
        if let Some((session, _)) = state.sessions.get(&session_id).await {
            crate::session::verify_csrf(&state.config, &headers, &session)?;
            id_token = session.id_token.clone();
        }
        // Unconditional: the user asked to end this session, and a refresh that
        // completes after this point must not bring it back.
        state.sessions.remove(&session_id).await;
    }

    let logout_url = state.keycloak.logout_url(id_token.as_deref())?;
    let mut response = Json(json!({ "logout_url": logout_url.as_str() })).into_response();
    let response_headers = response.headers_mut();
    response_headers.append(
        header::SET_COOKIE,
        clear_cookie(&state.config, &state.config.session_cookie, BROWSER_COOKIE_PATH, true),
    );
    response_headers.append(
        header::SET_COOKIE,
        clear_cookie(&state.config, &state.config.csrf_cookie, BROWSER_COOKIE_PATH, false),
    );
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::*;

    const UI: &str = "http://localhost:3000";

    #[test]
    fn a_missing_or_foreign_destination_falls_back_to_the_configured_ui() {
        for candidate in [
            None,
            Some(""),
            Some("   "),
            Some("http://evil.example/steal"),
            Some("https://localhost:3000/x"),
            Some("//evil.example/steal"),
            Some("javascript:alert(1)"),
            Some("http://localhost:3001/x"),
        ] {
            assert_eq!(safe_return_to(candidate, UI), UI, "{candidate:?}");
        }
    }

    #[test]
    fn same_origin_destinations_are_preserved() {
        assert_eq!(
            safe_return_to(Some("http://localhost:3000/etfs/VWCE-XETRA"), UI),
            "http://localhost:3000/etfs/VWCE-XETRA"
        );
    }

    /// Relative destinations used to be discarded: `Url::parse` rejects them, so
    /// every in-app deep link silently landed on the UI root after login.
    #[test]
    fn relative_destinations_resolve_against_the_ui_origin() {
        assert_eq!(
            safe_return_to(Some("/etfs/VWCE-XETRA?tab=audit"), UI),
            "http://localhost:3000/etfs/VWCE-XETRA?tab=audit"
        );
        assert_eq!(safe_return_to(Some("etfs"), UI), "http://localhost:3000/etfs");
    }

    #[test]
    fn pkce_challenges_are_the_urlsafe_unpadded_sha256_of_the_verifier() {
        // RFC 7636 appendix B.
        assert_eq!(
            pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        );
        assert!(!pkce_challenge("x").contains('='), "must be unpadded");
        assert!(!pkce_challenge("x").contains('+'), "must be URL-safe");
    }

    #[test]
    fn random_tokens_are_long_and_unique() {
        let a = random_token();
        assert_eq!(a.len(), 96);
        assert!(a.chars().all(|c| c.is_ascii_hexdigit()));
        assert_ne!(a, random_token());
    }

    #[tokio::test]
    async fn a_pending_login_is_one_time() {
        let store = PendingLoginStore::new(4);
        store.insert("id".into(), pending("s")).await;
        assert!(store.take("id").await.is_some());
        assert!(store.take("id").await.is_none(), "a replayed callback must find nothing");
    }

    /// The login-flow denial of service: an unauthenticated flood used to fill the
    /// map and make every subsequent login fail for the full ten-minute TTL.
    #[tokio::test]
    async fn a_flood_of_logins_cannot_lock_out_a_new_one() {
        let store = PendingLoginStore::new(8);
        for index in 0..500 {
            store.insert(format!("flood-{index}"), pending("flood")).await;
        }
        assert_eq!(store.len().await, 8, "capacity must still be enforced");

        store.insert("researcher".into(), pending("researcher")).await;
        let login = store.take("researcher").await.expect("the real login must survive");
        assert_eq!(login.state, "researcher");
    }

    #[tokio::test]
    async fn expired_pending_logins_are_reclaimed() {
        let store = PendingLoginStore::new(4);
        let mut stale = pending("stale");
        stale.created_at = Instant::now() - PENDING_LOGIN_TTL - Duration::from_secs(1);
        store.insert("stale".into(), stale).await;
        store.drop_expired().await;
        assert_eq!(store.len().await, 0);
    }

    fn pending(state: &str) -> PendingLogin {
        PendingLogin {
            state: state.into(),
            nonce: "nonce".into(),
            verifier: "verifier".into(),
            return_to: UI.into(),
            created_at: Instant::now(),
        }
    }
}
