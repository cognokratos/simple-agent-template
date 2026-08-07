# Security architecture

This variant protects every application hop:

```text
Browser
  │ Keycloak login; opaque HttpOnly BFF session + CSRF cookie
  ▼
assistant-ui / Next.js routes under /api/gateway
  │ server-side proxy; no OIDC token reaches browser JavaScript
  ▼
Rust authentication gateway (Compose-internal only)
  │ Authorization: Bearer ${AGENT_API_KEY}
  ▼
NeMo Agent Toolkit (Compose-internal only)
  │ Authorization: Bearer ${MCP_API_KEY}
  ▼
Rust MCP server (Compose-internal only)
```

Keycloak is the OpenID Connect provider. The Rust gateway is a backend-for-
frontend (BFF): the access token is used only for the initial UserInfo call, while refresh and ID tokens remain in gateway memory.
The browser receives only opaque cookies scoped to the UI's dedicated
`/api/gateway` namespace.

## Public and internal services

Normal Compose publishes only the browser-facing and operator interfaces, bound
to `PUBLIC_BIND_ADDRESS=127.0.0.1` by default:

| Service | URL | Purpose |
|---|---|---|
| assistant-ui | `http://localhost:3000` | User interface and BFF proxy routes |
| Keycloak | `http://localhost:8082` | OIDC login and administration |
| MLflow | `http://localhost:5000` | Local traces and evaluations |
| OTel health/OTLP | `13133`, `4318` | Local observability infrastructure |

The Rust gateway, NAT agent, and MCP server have no host-published ports in
`../docker-compose.yml`. The browser cannot bypass assistant-ui to call the gateway,
and neither the UI nor browser can call NAT or MCP directly.

For loopback-only diagnostics:

```bash
make debug-up
```

The debug override exposes:

```text
127.0.0.1:8081 → Rust gateway
127.0.0.1:8000 → NAT
127.0.0.1:8080 → MCP
```

## Browser authentication flow

1. The browser opens `/api/gateway/auth/login` on assistant-ui.
2. Next.js calls the internal gateway `/auth/login` route and relays its redirect
   and one-time transaction cookie.
3. The gateway creates OAuth state, nonce, and a PKCE S256 verifier, then redirects
   the browser to Keycloak.
4. Keycloak redirects to the assistant-ui callback
   `/api/gateway/auth/callback`.
5. Next.js forwards the callback query and transaction cookie to the internal
   gateway.
6. The gateway validates state, exchanges the authorization code over the
   Keycloak backchannel, validates the RS256 ID token (signature, issuer,
   audience, expiry, subject, and nonce), and fetches UserInfo.
7. The gateway stores the Keycloak tokens server-side and returns an opaque
   `HttpOnly` session cookie plus a separate CSRF cookie.

The session and CSRF cookies use `Path=/api/gateway`. This narrower
path avoids sending gateway credentials to unrelated localhost APIs such as
MLflow's `/api/2.0/...` endpoints. The login transaction cookie is narrower
still and is sent only to `/api/gateway/auth/callback`.

The session cookie is `HttpOnly`; the CSRF cookie is deliberately readable by
the Next.js server route and is checked through a double-submit header on chat
and logout requests. Cookies use `SameSite=Lax` or stricter. Set
`GATEWAY_COOKIE_SECURE=true` when the UI and callback use HTTPS.

The gateway stores sessions in memory for this local template. A replicated
production deployment should use a shared encrypted session store, explicit
revocation, and appropriate key rotation.

## Browser-facing UI route allowlist

assistant-ui exposes only the following gateway proxy routes:

```text
GET  /api/gateway/auth/login
GET  /api/gateway/auth/callback
GET  /api/gateway/auth/session
POST /api/gateway/auth/logout
POST /api/gateway/chat
```

The chat runtime is explicitly configured to use `/api/gateway/chat`; it does
not use the default `/api/chat` endpoint.

## Internal Rust gateway route allowlist

The gateway itself registers only:

```text
GET  /health
GET  /ready
GET  /auth/login
GET  /auth/callback
GET  /auth/session
POST /auth/logout
POST /api/chat
```

There is no generic reverse proxy. `/api/chat` always targets the configured NAT
workflow path and sets the allowed `filter_steps` itself. Client-supplied URL,
upstream, authorization, or trusted-identity headers are not forwarded.

The gateway accepts a strict JSON request containing only `messages`, permits
only `user` and `assistant` roles, requires the final message to be a user
message, rejects unknown properties, and enforces message-count and size limits.
SSE bytes are streamed without buffering.

## Keycloak development realm

`../keycloak/generate_realm.py` creates the realm import at startup. Defaults:

```text
Realm:        alerts
OIDC client:  alerts-gateway
User:         analyst
Password:     analyst
Realm role:   analyst
Callback:     http://localhost:3000/api/gateway/auth/callback
```

The client is confidential, uses Authorization Code flow with PKCE S256, and
disables implicit flow, direct password grants, device flow, CIBA, and service
accounts.

Change all development credentials and internal API keys before sharing or
deploying the stack. Copy `../.env.example` to `../.env`; the root `../.gitignore`
excludes `../.env` and other dotenv variants while keeping `../.env.example` tracked.
If the imported realm already exists and callback/client settings changed,
recreate Keycloak data:

```bash
make reset-auth
```

## Gateway to NAT authentication

The gateway sends:

```http
Authorization: Bearer ${AGENT_API_KEY}
```

A build-time NAT patch requires the same value from `NAT_GATEWAY_API_KEY` for
every route except health checks. The evaluator also uses this key when it calls
NAT directly inside the Compose network. After validation, NAT removes the
`Authorization` header from the ASGI scope before `SessionManager` captures
request metadata, preventing the service credential from entering MLflow traces.

The gateway creates trusted internal metadata headers itself:

```text
X-Request-Id
X-Authenticated-User-Id
X-Authenticated-Username
X-Authenticated-Email
X-Authenticated-Roles
```

These are generated from the authenticated Keycloak session, not accepted from
the browser.

## NAT to MCP authentication

NAT's streamable-HTTP MCP client uses:

```yaml
custom_headers:
  Authorization: Bearer ${MCP_API_KEY}
```

The MCP server compares the complete bearer value in constant time before
allowing `/mcp`, then removes it before RMCP handles the request. `/health`
remains unauthenticated for readiness checks.

## Verification

```bash
make security-config-test
make auth-test
make verify-mcp
make security-test
```

The checks verify that:

- the normal Compose topology publishes no gateway, NAT, or MCP ports;
- Keycloak and the gateway use the same UI callback;
- cookies are fixed to the narrow `/api/gateway` path;
- UI-proxied login redirects to Keycloak;
- unauthenticated gateway chat is rejected;
- the gateway does not expose arbitrary NAT paths;
- NAT rejects requests without `AGENT_API_KEY`;
- MCP rejects requests without `MCP_API_KEY` and accepts the configured key.
