//! Browser sessions and the locking discipline that keeps them honest.
//!
//! Sessions are held in memory, which means one gateway instance and a restart
//! logs everyone out. That is a deliberate limit of this deployment, not an
//! accident.
//!
//! What is *not* optional is the rule this module exists to enforce: a session
//! read, an await, and a write-back are three separate moments, and a write-back
//! may only land on the session it was derived from. Without that rule an
//! in-flight token refresh re-inserted a session that `logout` had already
//! removed during the await — so logging out did not reliably revoke anything,
//! and a captured session cookie kept working. Keycloak issues 5-minute access
//! tokens here, so an active user crosses the refresh boundary constantly and
//! that window is reached in normal use, not only under attack.

use std::{
    collections::HashMap,
    sync::Arc,
    time::{Duration, Instant},
};

use axum::http::HeaderMap;
use serde::Serialize;
use tokio::sync::{RwLock, Semaphore};

use crate::config::GatewayConfig;
use crate::cookies::cookie_value;
use crate::error::GatewayError;

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct UserIdentity {
    pub id: String,
    pub username: String,
    pub email: Option<String>,
    pub name: Option<String>,
    pub roles: Vec<String>,
}

#[derive(Clone)]
pub struct Session {
    pub user: UserIdentity,
    pub refresh_token: Option<String>,
    pub id_token: Option<String>,
    pub csrf_token: String,
    pub access_expires_at: Instant,
    pub session_expires_at: Instant,
    /// Concurrent proxied response streams this session may hold open. Shared by
    /// every clone of the session, so it survives refresh write-backs.
    pub stream_slots: Arc<Semaphore>,
}

impl Session {
    pub fn new(
        user: UserIdentity,
        csrf_token: String,
        refresh_token: Option<String>,
        id_token: Option<String>,
        access_ttl: Duration,
        session_ttl: Duration,
        max_streams: usize,
    ) -> Self {
        let now = Instant::now();
        Self {
            user,
            refresh_token,
            id_token,
            csrf_token,
            access_expires_at: now + access_ttl,
            session_expires_at: now + session_ttl,
            stream_slots: Arc::new(Semaphore::new(max_streams)),
        }
    }

    pub fn is_expired(&self, now: Instant) -> bool {
        self.session_expires_at <= now
    }

    /// Refresh slightly before the access token actually dies, so a request never
    /// races its own expiry.
    pub fn needs_refresh(&self, now: Instant) -> bool {
        self.access_expires_at <= now + Duration::from_secs(30)
    }
}

/// A session together with the version it was read at. Any write-back must quote
/// its generation, which is how a stale write is recognised and dropped.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Generation(u64);

struct Slot {
    session: Session,
    generation: Generation,
}

/// What happened to a write-back.
#[derive(Debug, PartialEq, Eq)]
pub enum WriteBack {
    /// The session was still the one that was read; the update landed.
    Applied,
    /// Another task updated the session first. Ours is stale and was discarded.
    Superseded,
    /// The session is gone — logged out or expired while we were awaiting. It must
    /// stay gone: re-inserting it is what made logout unreliable.
    Gone,
}

pub struct SessionStore {
    slots: RwLock<HashMap<String, Slot>>,
    max_sessions: usize,
    next_generation: std::sync::atomic::AtomicU64,
}

impl SessionStore {
    pub fn new(max_sessions: usize) -> Self {
        Self {
            slots: RwLock::new(HashMap::new()),
            max_sessions,
            next_generation: std::sync::atomic::AtomicU64::new(1),
        }
    }

    fn bump(&self) -> Generation {
        Generation(
            self.next_generation
                .fetch_add(1, std::sync::atomic::Ordering::Relaxed),
        )
    }

    pub async fn get(&self, session_id: &str) -> Option<(Session, Generation)> {
        let slots = self.slots.read().await;
        slots
            .get(session_id)
            .map(|slot| (slot.session.clone(), slot.generation))
    }

    /// Store a brand-new session, refusing once capacity is reached.
    pub async fn insert(&self, session_id: String, session: Session) -> Result<(), GatewayError> {
        let now = Instant::now();
        let mut slots = self.slots.write().await;
        slots.retain(|_, slot| !slot.session.is_expired(now));
        if slots.len() >= self.max_sessions {
            return Err(GatewayError::too_many_requests(
                "the gateway session capacity has been reached",
            ));
        }
        slots.insert(session_id, Slot { session, generation: self.bump() });
        Ok(())
    }

    /// Update a session only if it is still the one that was read.
    pub async fn replace_if_current(
        &self,
        session_id: &str,
        generation: Generation,
        session: Session,
    ) -> WriteBack {
        let mut slots = self.slots.write().await;
        match slots.get_mut(session_id) {
            None => WriteBack::Gone,
            Some(slot) if slot.generation != generation => WriteBack::Superseded,
            Some(slot) => {
                slot.session = session;
                slot.generation = self.bump();
                WriteBack::Applied
            }
        }
    }

    /// Revoke a session only if it is still the one that was read.
    ///
    /// A refresh that fails because a *concurrent* refresh already rotated the
    /// token must not log the user out: the session it would remove is a newer,
    /// working one.
    pub async fn remove_if_current(&self, session_id: &str, generation: Generation) -> WriteBack {
        let mut slots = self.slots.write().await;
        match slots.get(session_id) {
            None => WriteBack::Gone,
            Some(slot) if slot.generation != generation => WriteBack::Superseded,
            Some(_) => {
                slots.remove(session_id);
                WriteBack::Applied
            }
        }
    }

    /// Unconditional revocation, for logout: the caller's intent is to end the
    /// session whatever state it is in.
    pub async fn remove(&self, session_id: &str) -> Option<Session> {
        self.slots.write().await.remove(session_id).map(|slot| slot.session)
    }

    pub async fn drop_expired(&self) {
        let now = Instant::now();
        self.slots.write().await.retain(|_, slot| !slot.session.is_expired(now));
    }

    #[cfg(test)]
    pub async fn len(&self) -> usize {
        self.slots.read().await.len()
    }
}

pub fn session_id_from(config: &GatewayConfig, headers: &HeaderMap) -> Option<String> {
    cookie_value(headers, &config.session_cookie)
}

/// Three-way CSRF check: the double-submit cookie, the header echoing it, and the
/// token held server-side against this session. The third comparison is what makes
/// cookie shadowing useless — an attacker who can plant a cookie still cannot know
/// the session's own token.
pub fn verify_csrf(
    config: &GatewayConfig,
    headers: &HeaderMap,
    session: &Session,
) -> Result<(), GatewayError> {
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

pub fn constant_time_eq(left: &str, right: &str) -> bool {
    let (left, right) = (left.as_bytes(), right.as_bytes());
    if left.len() != right.len() {
        return false;
    }
    left.iter().zip(right.iter()).fold(0_u8, |diff, (a, b)| diff | (a ^ b)) == 0
}

#[cfg(test)]
mod tests {
    use super::*;

    fn identity(id: &str) -> UserIdentity {
        UserIdentity {
            id: id.into(),
            username: "researcher".into(),
            email: None,
            name: None,
            roles: vec!["researcher".into()],
        }
    }

    fn session(access_ttl: Duration) -> Session {
        Session::new(
            identity("user-1"),
            "csrf".into(),
            Some("refresh".into()),
            None,
            access_ttl,
            Duration::from_secs(3600),
            4,
        )
    }

    #[tokio::test]
    async fn a_write_back_lands_when_the_session_is_unchanged() {
        let store = SessionStore::new(10);
        store.insert("s1".into(), session(Duration::from_secs(300))).await.expect("inserted");
        let (mut read, generation) = store.get("s1").await.expect("present");

        read.access_expires_at = Instant::now() + Duration::from_secs(900);
        assert_eq!(store.replace_if_current("s1", generation, read).await, WriteBack::Applied);
        assert_eq!(store.len().await, 1);
    }

    /// The defect this module exists to prevent: a refresh in flight while the
    /// user logs out used to re-insert the session it had cloned, leaving a
    /// revoked session usable by anyone holding its cookie.
    #[tokio::test]
    async fn a_write_back_never_resurrects_a_logged_out_session() {
        let store = SessionStore::new(10);
        store.insert("s1".into(), session(Duration::from_secs(1))).await.expect("inserted");
        let (read, generation) = store.get("s1").await.expect("present");

        // ... the refresh is awaiting Keycloak here, and logout lands.
        store.remove("s1").await;

        assert_eq!(store.replace_if_current("s1", generation, read).await, WriteBack::Gone);
        assert!(store.get("s1").await.is_none(), "the session came back from the dead");
    }

    /// Two requests crossing the 5-minute access-token boundary together. The
    /// loser's write must not clobber the winner's newer session.
    #[tokio::test]
    async fn a_stale_write_back_is_discarded_rather_than_clobbering() {
        let store = SessionStore::new(10);
        store.insert("s1".into(), session(Duration::from_secs(1))).await.expect("inserted");
        let (first, first_generation) = store.get("s1").await.expect("present");
        let (second, second_generation) = store.get("s1").await.expect("present");
        assert_eq!(first_generation, second_generation);

        let mut winner = first;
        winner.refresh_token = Some("rotated-by-winner".into());
        assert_eq!(
            store.replace_if_current("s1", first_generation, winner).await,
            WriteBack::Applied
        );

        let mut loser = second;
        loser.refresh_token = Some("stale".into());
        assert_eq!(
            store.replace_if_current("s1", second_generation, loser).await,
            WriteBack::Superseded
        );

        let (current, _) = store.get("s1").await.expect("present");
        assert_eq!(current.refresh_token.as_deref(), Some("rotated-by-winner"));
    }

    /// With refresh-token rotation enabled the loser's grant is rejected. Removing
    /// the session on that failure would log the user out even though the
    /// winner just installed a working one.
    #[tokio::test]
    async fn a_losing_refresh_failure_does_not_revoke_the_winners_session() {
        let store = SessionStore::new(10);
        store.insert("s1".into(), session(Duration::from_secs(1))).await.expect("inserted");
        let (_, stale_generation) = store.get("s1").await.expect("present");

        let (winner, winner_generation) = store.get("s1").await.expect("present");
        store.replace_if_current("s1", winner_generation, winner).await;

        assert_eq!(
            store.remove_if_current("s1", stale_generation).await,
            WriteBack::Superseded
        );
        assert!(store.get("s1").await.is_some(), "the user was logged out by a stale failure");
    }

    #[tokio::test]
    async fn a_genuine_refresh_failure_revokes_the_session() {
        let store = SessionStore::new(10);
        store.insert("s1".into(), session(Duration::from_secs(1))).await.expect("inserted");
        let (_, generation) = store.get("s1").await.expect("present");
        assert_eq!(store.remove_if_current("s1", generation).await, WriteBack::Applied);
        assert!(store.get("s1").await.is_none());
    }

    #[tokio::test]
    async fn capacity_is_enforced_but_expired_sessions_are_reclaimed_first() {
        let store = SessionStore::new(2);
        store.insert("a".into(), session(Duration::from_secs(300))).await.expect("inserted");
        let mut dead = session(Duration::from_secs(300));
        dead.session_expires_at = Instant::now() - Duration::from_secs(1);
        store.insert("b".into(), dead).await.expect("inserted");

        // "b" is expired, so it is reclaimed to make room rather than refusing.
        store.insert("c".into(), session(Duration::from_secs(300))).await.expect("inserted");
        assert!(store.get("b").await.is_none());
        assert!(store.get("c").await.is_some());

        assert!(store.insert("d".into(), session(Duration::from_secs(300))).await.is_err());
    }

    #[test]
    fn refresh_is_due_shortly_before_the_access_token_expires() {
        let now = Instant::now();
        let fresh = session(Duration::from_secs(300));
        assert!(!fresh.needs_refresh(now));
        let nearly = session(Duration::from_secs(10));
        assert!(nearly.needs_refresh(now));
    }

    #[test]
    fn token_comparison_is_length_safe() {
        assert!(constant_time_eq("abc", "abc"));
        assert!(!constant_time_eq("abc", "abd"));
        assert!(!constant_time_eq("abc", "ab"));
        assert!(!constant_time_eq("", "a"));
        assert!(constant_time_eq("", ""));
    }

    fn csrf_headers(cookie: &str, header: &str) -> HeaderMap {
        let mut headers = HeaderMap::new();
        headers.insert(
            axum::http::header::COOKIE,
            format!("etf_research_gateway_csrf={cookie}").parse().expect("valid"),
        );
        headers.insert("x-csrf-token", header.parse().expect("valid"));
        headers
    }

    #[test]
    fn csrf_requires_cookie_header_and_session_token_to_agree() {
        let config = crate::config::test_support::config();
        let session = session(Duration::from_secs(300));

        assert!(verify_csrf(&config, &csrf_headers("csrf", "csrf"), &session).is_ok());
        // A planted cookie the attacker also echoes still fails: it is not the
        // session's token.
        assert!(verify_csrf(&config, &csrf_headers("planted", "planted"), &session).is_err());
        assert!(verify_csrf(&config, &csrf_headers("csrf", "other"), &session).is_err());
        assert!(verify_csrf(&config, &HeaderMap::new(), &session).is_err());
    }
}
