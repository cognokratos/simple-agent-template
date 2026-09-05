//! Reading and minting the three browser cookies this gateway owns.

use axum::http::{header, HeaderMap, HeaderValue};

use crate::config::{GatewayConfig, BROWSER_COOKIE_PATH, PENDING_LOGIN_TTL};

/// First value for `name` in the request's Cookie header.
///
/// A client can present several cookies with the same name — different paths or
/// domains — and the browser sends the most specific first. Taking the first
/// match is therefore right, and it is safe against cookie shadowing here because
/// every use is checked against server-side state: a shadowed session ID resolves
/// to no session, and a shadowed CSRF cookie still has to equal the session's own
/// token.
pub fn cookie_value(headers: &HeaderMap, name: &str) -> Option<String> {
    let cookie = headers.get(header::COOKIE)?.to_str().ok()?;
    cookie.split(';').find_map(|part| {
        let (key, value) = part.trim().split_once('=')?;
        (key == name).then(|| value.to_owned())
    })
}

pub fn session_cookie(config: &GatewayConfig, value: &str) -> HeaderValue {
    header_value(format!(
        "{}={}; Path={}; Max-Age={}; HttpOnly; SameSite=Lax{}",
        config.session_cookie,
        value,
        BROWSER_COOKIE_PATH,
        config.session_ttl.as_secs(),
        config.cookie_suffix(),
    ))
}

/// Readable by the UI on purpose: the double-submit half of CSRF defence. Its
/// value is worthless without the matching server-side session token.
pub fn csrf_cookie(config: &GatewayConfig, value: &str) -> HeaderValue {
    header_value(format!(
        "{}={}; Path={}; Max-Age={}; SameSite=Strict{}",
        config.csrf_cookie,
        value,
        BROWSER_COOKIE_PATH,
        config.session_ttl.as_secs(),
        config.cookie_suffix(),
    ))
}

pub fn login_cookie(config: &GatewayConfig, value: &str) -> HeaderValue {
    header_value(format!(
        "{}={}; Path={}; Max-Age={}; HttpOnly; SameSite=Lax{}",
        config.login_cookie,
        value,
        config.callback_cookie_path(),
        PENDING_LOGIN_TTL.as_secs(),
        config.cookie_suffix(),
    ))
}

pub fn clear_cookie(
    config: &GatewayConfig,
    name: &str,
    path: &str,
    http_only: bool,
) -> HeaderValue {
    header_value(format!(
        "{}=; Path={}; Max-Age=0; {}SameSite=Lax{}",
        name,
        path,
        if http_only { "HttpOnly; " } else { "" },
        config.cookie_suffix(),
    ))
}

/// Cookie names are validated at startup and every value here is an opaque hex
/// token this service minted, so this cannot fail in practice.
fn header_value(cookie: String) -> HeaderValue {
    HeaderValue::from_str(&cookie).expect("gateway cookies are built from validated components")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::test_support::config;

    fn headers_with(cookie: &str) -> HeaderMap {
        let mut headers = HeaderMap::new();
        headers.insert(header::COOKIE, HeaderValue::from_str(cookie).expect("valid"));
        headers
    }

    #[test]
    fn cookie_lookup_matches_whole_names_only() {
        let headers = headers_with("other=1; etf_research_gateway_session=abc123; trailing=2");
        assert_eq!(cookie_value(&headers, "etf_research_gateway_session"), Some("abc123".into()));
        // A name that merely contains the target must not match.
        assert_eq!(cookie_value(&headers, "gateway_session"), None);
        assert_eq!(cookie_value(&headers, "etf_research_gateway_csrf"), None);
        assert_eq!(cookie_value(&HeaderMap::new(), "etf_research_gateway_session"), None);
    }

    #[test]
    fn cookie_values_may_contain_equals_signs() {
        let headers = headers_with("etf_research_gateway_csrf=a=b=c");
        assert_eq!(cookie_value(&headers, "etf_research_gateway_csrf"), Some("a=b=c".into()));
    }

    #[test]
    fn the_session_cookie_is_http_only_and_scoped_to_the_bff_path() {
        let cookie = session_cookie(&config(), "token").to_str().expect("ascii").to_owned();
        assert!(cookie.contains("HttpOnly"), "{cookie}");
        assert!(cookie.contains("SameSite=Lax"), "{cookie}");
        assert!(cookie.contains("Path=/api/gateway"), "{cookie}");
        assert!(!cookie.contains("Secure"), "insecure transport was configured");
    }

    /// The CSRF cookie must stay script-readable — the UI has to echo it back into
    /// the x-csrf-token header — and Strict so it never rides a cross-site request.
    #[test]
    fn the_csrf_cookie_is_readable_and_strict() {
        let cookie = csrf_cookie(&config(), "token").to_str().expect("ascii").to_owned();
        assert!(!cookie.contains("HttpOnly"), "{cookie}");
        assert!(cookie.contains("SameSite=Strict"), "{cookie}");
    }

    /// The login cookie exists only for the callback hop, so it is scoped to that
    /// one path and expires with the pending login it names.
    #[test]
    fn the_login_cookie_is_narrow_and_short_lived() {
        let cookie = login_cookie(&config(), "token").to_str().expect("ascii").to_owned();
        assert!(cookie.contains("Path=/api/gateway/auth/callback"), "{cookie}");
        assert!(cookie.contains("HttpOnly"), "{cookie}");
        assert!(
            cookie.contains(&format!("Max-Age={}", PENDING_LOGIN_TTL.as_secs())),
            "the cookie must not outlive the server-side pending login: {cookie}"
        );
    }

    #[test]
    fn secure_transport_adds_the_secure_attribute_everywhere() {
        let config = GatewayConfig { cookie_secure: true, ..config() };
        for cookie in [
            session_cookie(&config, "t"),
            csrf_cookie(&config, "t"),
            login_cookie(&config, "t"),
            clear_cookie(&config, "n", "/", true),
        ] {
            assert!(cookie.to_str().expect("ascii").contains("; Secure"), "{cookie:?}");
        }
    }

    #[test]
    fn clearing_a_cookie_expires_it_on_the_path_that_set_it() {
        let cookie = clear_cookie(&config(), "etf_research_gateway_session", BROWSER_COOKIE_PATH, true);
        let cookie = cookie.to_str().expect("ascii");
        assert!(cookie.starts_with("etf_research_gateway_session=;"), "{cookie}");
        assert!(cookie.contains("Max-Age=0"), "{cookie}");
        assert!(cookie.contains("Path=/api/gateway"), "{cookie}");
    }
}
