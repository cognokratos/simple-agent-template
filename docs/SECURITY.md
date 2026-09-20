# Security

Trust boundaries are in [ARCHITECTURE.md](ARCHITECTURE.md). This describes what
each control does and how to check it.

## Browser authentication

Keycloak Authorization Code flow with PKCE (S256), run entirely by the Rust
gateway. The browser never holds an OIDC token.

* **PKCE** — verifier held server-side in a pending-login entry, challenge sent
  to the authorization endpoint.
* **`state`** — compared in constant time; the pending login is removed *before*
  validation, so a replayed callback finds nothing.
* **`nonce`** — required in the ID token and compared in constant time.
* **ID token** — RS256 only, signature checked against cached JWKS, issuer
  pinned to the *public* realm URL (what the browser was redirected to, not the
  internal URL the gateway dials), audience pinned to the client id, `exp`
  enforced.

### Cookies

| Cookie | Attributes | Why |
| --- | --- | --- |
| session | `HttpOnly`, `SameSite=Lax`, `Path=/api/gateway` | Opaque id; the tokens stay server-side |
| CSRF | readable, `SameSite=Strict`, `Path=/api/gateway` | The UI must echo it into `x-csrf-token` |
| login | `HttpOnly`, `SameSite=Lax`, `Path=/api/gateway/auth/callback` | Exists only for the callback hop |

`Secure` is added to all of them when `GATEWAY_COOKIE_SECURE=true`. That flag is
parsed strictly: an unrecognised value is an error rather than silently `false`,
because resolving a security flag toward the weaker setting on a typo is how
`GATEWAY_COOKIE_SECURE=Ture` ships cookies without `Secure`.

### CSRF

Three-way: the double-submit cookie, the header echoing it, and the token held
server-side for that session. The third comparison is what makes cookie
shadowing useless — an attacker who can plant a cookie still cannot know the
session's own token.

## Session handling

Sessions are in memory: one gateway instance, and a restart logs everyone out.
That is a deliberate limit of this deployment.

What is not optional is the write-back rule. A session read, an await, and a
write-back are three separate moments, and a write-back may only land on the
session it was derived from. Every update quotes the generation it read and
resolves to `Applied`, `Superseded` or `Gone`. Without it:

* a token refresh in flight during logout re-inserts the session logout just
  removed, so logging out does not reliably revoke anything;
* two requests crossing the access-token boundary let the loser's stale tokens
  overwrite the winner's;
* with refresh-token rotation the loser's grant is rejected, and revoking on
  that failure logs the user out even though the winner just installed a working
  session.

Keycloak issues 5-minute access tokens here, so an active user crosses that
boundary constantly; this is reached in normal use, not only under attack.

Roles are re-read from `userinfo` on every refresh. They used to be captured
once at login and forwarded unchanged for the whole 8-hour session TTL, so a
role revoked in Keycloak stayed in effect.

## Service credentials

| From | To | Variable | Notes |
| --- | --- | --- | --- |
| gateway | NAT | `AGENT_API_KEY` → `NAT_GATEWAY_API_KEY` | Constant-time compare; stripped from the ASGI scope after validation so NAT session metadata and telemetry never see it |
| evaluator | NAT | `AGENT_API_KEY` | Same endpoint, bypassing the browser path |
| NAT | MCP | `MCP_API_KEY` | Constant-time compare; removed from the request before RMCP logging |

`make security-config-test` asserts the gateway, evaluator and NAT agree on
`AGENT_API_KEY`, and NAT and MCP on `MCP_API_KEY`, from the *resolved* Compose
configuration.

## Request validation

The gateway re-serializes every chat request from its parsed schema, so anything
the browser sent beyond the declared fields does not survive. `deny_unknown_fields`
means an extra key is an error rather than a passthrough. Only `user` and
`assistant` roles are accepted — a `system` or `tool` role from the browser would
be an instruction channel straight into the prompt. Message count, per-message
characters and total characters are all bounded, counted in characters rather
than bytes.

## Availability

* **Per-session concurrent streams** (`GATEWAY_MAX_STREAMS_PER_SESSION`, default
  4). The permit lives inside the response stream, so it is released on
  completion, error and browser disconnect alike.
* **Pending logins** are bounded and evict oldest-first rather than refusing the
  newest. `/auth/login` needs no credentials and each call held a slot for ten
  minutes, so a thousand anonymous calls used to lock every user out for that
  long.
* **Explicit upstream timeouts** on every non-streaming call
  (`GATEWAY_UPSTREAM_TIMEOUT_SECONDS`, default 10). `connect_timeout` covers only
  TCP setup, so a Keycloak that accepts the connection and then stalls used to
  hang the request — and since refresh sits inside session authentication, that
  hung every authenticated request. The proxied chat response deliberately has
  **no** request timeout: it is a long-lived event stream.
* **JWKS** cached for 5 minutes; a forced refetch on an unknown `kid` is floored
  at 30 seconds, so an attacker cannot turn invented key ids into unbounded load
  on Keycloak.

## Error handling

Upstream error bodies are not relayed to the browser. A reqwest error embeds
`http://keycloak:8080`, and a Keycloak rejection body describes the realm; this
is an unauthenticated boundary and those strings describe internal topology. The
caller learns which dependency failed, the log keeps the cause.

## Identity headers

The gateway builds a **fresh** upstream request and forwards no browser header.
Identity is minted from the validated session:

```
x-authenticated-user-id, x-authenticated-username,
x-authenticated-roles, x-authenticated-email
```

Values outside printable ASCII are percent-encoded rather than dropped. The
previous helper silently omitted a header it could not encode, so a user whose
Keycloak display name contained an accent reached the agent with identity
headers missing — a security-relevant field disappearing with no error anywhere.

`x-authenticated-email` is redacted from telemetry; see
[OBSERVABILITY.md](OBSERVABILITY.md).

## Verifying it

```
make static-check     # offline: topology, source wiring, contracts
make security-test    # + live: network isolation, auth boundaries, MCP keys
make network-test     # runtime east-west reachability only
```

`make security-config-test` renders the Compose configuration with **every**
profile enabled. Without that, `docker compose config` omits profile-gated
services and the check silently skips them.

## Known limitations

See [LIMITATIONS.md](LIMITATIONS.md). Production hardening this template does
not do is listed there rather than implied to be done.
