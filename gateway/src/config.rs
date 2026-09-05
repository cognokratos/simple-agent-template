//! Everything the gateway reads from its environment, validated once at startup.

use std::time::Duration;

use anyhow::{Context, Result};
use url::Url;

pub const DEFAULT_SESSION_COOKIE: &str = "etf_research_gateway_session";
pub const DEFAULT_CSRF_COOKIE: &str = "etf_research_gateway_csrf";
pub const DEFAULT_LOGIN_COOKIE: &str = "etf_research_gateway_login";
pub const BROWSER_COOKIE_PATH: &str = "/api/gateway";
pub const OIDC_CALLBACK_PATH: &str = "/api/gateway/auth/callback";

/// How long a login may sit half-finished between the redirect and the callback.
pub const PENDING_LOGIN_TTL: Duration = Duration::from_secs(600);

#[derive(Clone)]
pub struct GatewayConfig {
    pub bind_address: String,
    pub ui_public_url: String,
    pub oidc_callback_url: String,
    pub keycloak_public_url: String,
    pub keycloak_internal_url: String,
    pub keycloak_realm: String,
    pub oidc_client_id: String,
    pub oidc_client_secret: String,
    pub agent_workflow_url: String,
    pub agent_api_key: String,
    pub agent_filter_steps: String,
    pub cookie_secure: bool,
    pub session_ttl: Duration,
    /// Ceiling on any single non-streaming upstream call. Without one, a
    /// dependency that accepts the connection and then stalls hangs the request
    /// forever: `connect_timeout` only covers TCP setup.
    pub upstream_timeout: Duration,
    pub max_sessions: usize,
    pub max_pending_logins: usize,
    pub max_streams_per_session: usize,
    pub max_messages: usize,
    pub max_message_chars: usize,
    pub max_total_message_chars: usize,
    pub session_cookie: String,
    pub csrf_cookie: String,
    pub login_cookie: String,
}

impl GatewayConfig {
    pub fn from_env() -> Result<Self> {
        let config = Self {
            bind_address: env_or("GATEWAY_BIND_ADDRESS", "0.0.0.0:8081"),
            ui_public_url: env_or("UI_PUBLIC_URL", "http://localhost:3000"),
            oidc_callback_url: env_or(
                "OIDC_CALLBACK_URL",
                "http://localhost:3000/api/gateway/auth/callback",
            ),
            keycloak_public_url: env_or("KEYCLOAK_PUBLIC_URL", "http://localhost:8082"),
            keycloak_internal_url: env_or("KEYCLOAK_INTERNAL_URL", "http://keycloak:8080"),
            keycloak_realm: env_or("KEYCLOAK_REALM", "etf-research"),
            oidc_client_id: env_or("KEYCLOAK_GATEWAY_CLIENT_ID", "etf-research-gateway"),
            oidc_client_secret: required_env("KEYCLOAK_GATEWAY_CLIENT_SECRET")?,
            agent_workflow_url: env_or("AGENT_WORKFLOW_URL", "http://agent:8000/v1/workflow/full"),
            agent_api_key: required_env("AGENT_API_KEY")?,
            agent_filter_steps: env_or(
                "AGENT_FILTER_STEPS",
                "TOOL_START,TOOL_END,FUNCTION_START,FUNCTION_END",
            ),
            cookie_secure: env_bool("GATEWAY_COOKIE_SECURE", false)?,
            session_ttl: Duration::from_secs(env_u64("GATEWAY_SESSION_TTL_SECONDS", 28_800)?),
            upstream_timeout: Duration::from_secs(env_u64("GATEWAY_UPSTREAM_TIMEOUT_SECONDS", 10)?),
            max_sessions: env_usize("GATEWAY_MAX_SESSIONS", 1_000)?,
            max_pending_logins: env_usize("GATEWAY_MAX_PENDING_LOGINS", 1_000)?,
            max_streams_per_session: env_usize("GATEWAY_MAX_STREAMS_PER_SESSION", 4)?,
            max_messages: env_usize("GATEWAY_MAX_MESSAGES", 100)?,
            max_message_chars: env_usize("GATEWAY_MAX_MESSAGE_CHARS", 32_768)?,
            max_total_message_chars: env_usize("GATEWAY_MAX_TOTAL_MESSAGE_CHARS", 262_144)?,
            session_cookie: env_or("GATEWAY_SESSION_COOKIE", DEFAULT_SESSION_COOKIE),
            csrf_cookie: env_or("GATEWAY_CSRF_COOKIE", DEFAULT_CSRF_COOKIE),
            login_cookie: env_or("GATEWAY_LOGIN_COOKIE", DEFAULT_LOGIN_COOKIE),
        };
        config.validate()?;
        Ok(config)
    }

    fn validate(&self) -> Result<()> {
        let ui_url = Url::parse(&self.ui_public_url).context("UI_PUBLIC_URL must be a valid URL")?;
        let callback_url =
            Url::parse(&self.oidc_callback_url).context("OIDC_CALLBACK_URL must be a valid URL")?;
        anyhow::ensure!(
            same_origin(&ui_url, &callback_url),
            "OIDC_CALLBACK_URL must use the same origin as UI_PUBLIC_URL because the UI proxies the BFF callback"
        );
        anyhow::ensure!(
            callback_url.path() == OIDC_CALLBACK_PATH,
            "OIDC_CALLBACK_URL path must be exactly {OIDC_CALLBACK_PATH}"
        );
        Url::parse(&self.keycloak_public_url).context("KEYCLOAK_PUBLIC_URL must be a valid URL")?;
        Url::parse(&self.keycloak_internal_url)
            .context("KEYCLOAK_INTERNAL_URL must be a valid URL")?;
        Url::parse(&self.agent_workflow_url).context("AGENT_WORKFLOW_URL must be a valid URL")?;
        anyhow::ensure!(
            !self.upstream_timeout.is_zero(),
            "GATEWAY_UPSTREAM_TIMEOUT_SECONDS must be greater than zero"
        );
        for (name, value) in [
            ("GATEWAY_SESSION_COOKIE", self.session_cookie.as_str()),
            ("GATEWAY_CSRF_COOKIE", self.csrf_cookie.as_str()),
            ("GATEWAY_LOGIN_COOKIE", self.login_cookie.as_str()),
        ] {
            anyhow::ensure!(valid_cookie_name(value), "{name} must be a valid cookie name");
        }
        Ok(())
    }

    pub fn callback_url(&self) -> &str {
        &self.oidc_callback_url
    }

    pub fn callback_cookie_path(&self) -> &'static str {
        OIDC_CALLBACK_PATH
    }

    pub fn realm_endpoint(&self, base: &str, suffix: &str) -> String {
        format!(
            "{}/realms/{}/{}",
            base.trim_end_matches('/'),
            self.keycloak_realm,
            suffix.trim_start_matches('/')
        )
    }

    pub fn cookie_suffix(&self) -> &'static str {
        if self.cookie_secure { "; Secure" } else { "" }
    }
}

pub fn same_origin(left: &Url, right: &Url) -> bool {
    left.scheme() == right.scheme()
        && left.host_str() == right.host_str()
        && left.port_or_known_default() == right.port_or_known_default()
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
    anyhow::ensure!(!value.is_empty(), "{name} must not be empty");
    Ok(value.to_owned())
}

pub fn valid_cookie_name(name: &str) -> bool {
    !name.is_empty()
        && name.bytes().all(|byte| {
            byte.is_ascii_alphanumeric()
                || matches!(
                    byte,
                    b'!' | b'#' | b'$' | b'%' | b'&' | b'\'' | b'*' | b'+' | b'-' | b'.' | b'^'
                        | b'_' | b'`' | b'|' | b'~'
                )
        })
}

/// Parse a boolean environment value, refusing anything ambiguous.
///
/// This used to map every unrecognised value — including `Ture`, `TRUE `, or an
/// accidental empty string — to `false`. For `GATEWAY_COOKIE_SECURE` that means a
/// typo silently ships cookies without the `Secure` attribute. A security flag
/// must never resolve a misconfiguration toward the weaker setting.
pub fn parse_bool(value: &str) -> Option<bool> {
    match value.trim().to_ascii_lowercase().as_str() {
        "1" | "true" | "yes" | "on" => Some(true),
        "0" | "false" | "no" | "off" => Some(false),
        _ => None,
    }
}

fn env_bool(name: &str, default: bool) -> Result<bool> {
    match std::env::var(name) {
        Err(_) => Ok(default),
        Ok(value) if value.trim().is_empty() => Ok(default),
        Ok(value) => parse_bool(&value)
            .with_context(|| format!("{name} must be one of true/false/yes/no/on/off/1/0")),
    }
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
    anyhow::ensure!(value > 0, "{name} must be greater than zero");
    Ok(value)
}

#[cfg(test)]
pub mod test_support {
    use super::*;

    /// A fully-populated configuration for tests that need one but are not testing
    /// configuration itself.
    pub fn config() -> GatewayConfig {
        GatewayConfig {
            bind_address: "0.0.0.0:8081".into(),
            ui_public_url: "http://localhost:3000".into(),
            oidc_callback_url: "http://localhost:3000/api/gateway/auth/callback".into(),
            keycloak_public_url: "http://localhost:8082".into(),
            keycloak_internal_url: "http://keycloak:8080".into(),
            keycloak_realm: "etf-research".into(),
            oidc_client_id: "etf-research-gateway".into(),
            oidc_client_secret: "secret".into(),
            agent_workflow_url: "http://agent:8000/v1/workflow/full".into(),
            agent_api_key: "key".into(),
            agent_filter_steps: "TOOL_START".into(),
            cookie_secure: false,
            session_ttl: Duration::from_secs(28_800),
            upstream_timeout: Duration::from_secs(10),
            max_sessions: 10,
            max_pending_logins: 10,
            max_streams_per_session: 4,
            max_messages: 100,
            max_message_chars: 32_768,
            max_total_message_chars: 262_144,
            session_cookie: DEFAULT_SESSION_COOKIE.into(),
            csrf_cookie: DEFAULT_CSRF_COOKIE.into(),
            login_cookie: DEFAULT_LOGIN_COOKIE.into(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The shipped defaults must satisfy the same validation as any override.
    #[test]
    fn the_default_configuration_validates() {
        test_support::config().validate().expect("defaults must be valid");
    }

    #[test]
    fn the_callback_must_share_the_ui_origin_and_exact_path() {
        let mut config = test_support::config();
        config.oidc_callback_url = "http://evil.example/api/gateway/auth/callback".into();
        assert!(config.validate().is_err(), "cross-origin callback accepted");

        let mut config = test_support::config();
        config.oidc_callback_url = "http://localhost:3000/somewhere/else".into();
        assert!(config.validate().is_err(), "unexpected callback path accepted");
    }

    #[test]
    fn cookie_names_reject_anything_that_would_break_the_header() {
        for name in ["etf_research_gateway_session", "a", "x-y.z", "A1!#$%&'*+-.^_`|~"] {
            assert!(valid_cookie_name(name), "{name}");
        }
        for name in ["", "has space", "has=equals", "has;semi", "quoted\"", "tab\t", "unicodé"] {
            assert!(!valid_cookie_name(name), "{name:?}");
        }
    }

    #[test]
    fn boolean_configuration_refuses_ambiguity_rather_than_weakening() {
        for value in ["1", "true", "TRUE", " yes ", "on"] {
            assert_eq!(parse_bool(value), Some(true), "{value:?}");
        }
        for value in ["0", "false", "No", "off"] {
            assert_eq!(parse_bool(value), Some(false), "{value:?}");
        }
        // The dangerous ones: each of these previously resolved to `false`, which
        // for GATEWAY_COOKIE_SECURE silently drops the Secure attribute.
        for value in ["Ture", "y", "enabled", "2", "-", "sure"] {
            assert_eq!(parse_bool(value), None, "{value:?}");
        }
    }

    #[test]
    fn origins_compare_by_scheme_host_and_effective_port() {
        let base = Url::parse("http://localhost:3000").expect("valid");
        for same in ["http://localhost:3000/x", "http://localhost:3000/api/gateway"] {
            assert!(same_origin(&base, &Url::parse(same).expect("valid")), "{same}");
        }
        for other in [
            "https://localhost:3000/",
            "http://evil.example/",
            "http://localhost:3001/",
            "http://localhost.evil.example/",
        ] {
            assert!(!same_origin(&base, &Url::parse(other).expect("valid")), "{other}");
        }
        // Default ports are equivalent to their explicit form.
        let http = Url::parse("http://example.test").expect("valid");
        let explicit = Url::parse("http://example.test:80/a").expect("valid");
        assert!(same_origin(&http, &explicit));
    }
}
