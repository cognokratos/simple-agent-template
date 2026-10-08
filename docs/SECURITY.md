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
| gateway | agent | `AGENT_API_KEY` → `NAT_GATEWAY_API_KEY` | Constant-time compare, then removed before anything downstream sees it: NAT strips it from the ASGI scope so its session metadata and telemetry never see it; the Rig agent removes the header in its auth middleware before any handler, log or span |
| evaluator | agent | `AGENT_API_KEY` | Same endpoint, bypassing the browser path |
| agent | MCP | `MCP_API_KEY` | Constant-time compare; removed from the request before RMCP logging |

Both agents read the same variable names, including the historical
`NAT_GATEWAY_API_KEY`, so the two branches' configurations compare line for line.

`make security-config-test` asserts the gateway, evaluator and agent agree on
`AGENT_API_KEY`, and the agent and MCP on `MCP_API_KEY`, from the *resolved* Compose
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

Neither agent exports the raw identity. NAT copies these headers into span
metadata and `main` redacts the user id, username and email from exported
telemetry in every mode; the Rig agent never records them, keeping only the
roles (`IDENTITY_HEADERS` / `RETAINED_GATEWAY_HEADERS` in
[`identity.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/identity.rs)). Per-user attribution, when enabled,
is a pseudonym and nothing else. See
[OBSERVABILITY.md](OBSERVABILITY.md#redaction-and-what-it-does-not-cover).

### The agent requires an asserted identity

Both agents answer two questions, in this order, on every route except
liveness: *is this the gateway?* (the service credential) and *who is it acting
for?* (exactly one non-empty `x-authenticated-user-id`, otherwise `401`).
`make auth-test` asserts the same four statuses against either agent. How each
gets there differs.

#### On the NAT implementation

Two layers, because NAT's own one does not reach the client.

`general.front_end.identity_header: x-authenticated-user-id` in
`agent/config.yml` is NAT 1.9's supported way to consume an identity asserted by
a trusted proxy. It resolves the header into a `UserInfo` and publishes it as
`Context.user_id`, which is what makes per-user span attribution possible.

It is **not** what refuses a request that asserts nothing. NAT raises
`IdentityHeaderError` and registers a handler that would turn it into a `401`,
but `add_generate_routes` passes `enable_interactive=True` unconditionally for
the workflow path and its `/stream` and `/full` variants — the
`enable_interactive_extensions` setting only governs whether the
`/executions/...` endpoints are mounted, not which runner serves the workflow.
The interactive runner acquires the session in a background task after the
response has begun, inside a blanket `except Exception` that pushes the error
into the stream body. Measured on 1.9.0: a keyed request with no identity header
returns **200** with a `WORKFLOW_ERROR` event while the agent log shows
`IdentityHeaderError: Configured identity header 'x-authenticated-user-id' is
missing`.

`RequireIdentityHeaderMiddleware` in `fastapi_worker.py` is therefore what
actually enforces it: pure ASGI, ahead of NAT, requiring exactly one non-empty
occurrence on every non-health route and answering `401` otherwise. `make
auth-test` asserts that `401`, so the control cannot quietly revert to advisory.

Both halves of the rule matter:

* **Missing.** Previously a caller holding the service credential could reach
  the workflow with no identity at all. The approval module refused to mint a
  token in that state, but nothing stopped the request earlier, and the refusal
  was the only thing standing between an unattributed request and an
  unattributed audit record.
* **Repeated.** A repeated header is ambiguous, not a list. Accepting the first
  occurrence would let anything able to append a header decide who the user is.
  `ResponderIdentityMiddleware` applies the same exactly-once rule to approval
  responses, so both sides of an ownership check agree about the same request.

This does **not** replace the service credential, and enabling it without one
would be a mistake. NAT's own guidance is that a trusted identity header is only
sound where untrusted clients cannot reach the server directly and the proxy
strips any client-supplied value. Network reachability answers "can this packet
arrive"; only the credential answers "is this caller the gateway". The two are
complementary layers over the same question, and `make auth-test` asserts each
independently — no key, key without identity, key with a repeated identity, and
key with a well-formed identity.

#### On the Rig implementation

[`api/auth.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/api/auth.rs) is the service's own Axum middleware,
so there is no framework refusal to work around: the service key, then exactly
one bounded identity header, then at most one `x-request-id` (a repeated one is
`400`, because an approval token is bound to it). Only then is a
[`TrustedCaller`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/identity.rs) attached to the request. It has no
public constructor, so no other code path — and nothing the model produces — can
create one; it is never rendered into the prompt, and an MCP tool whose schema
declares `user_id`, `actor_id`, `approval_token` or similar is refused at startup
([`mcp/schema.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/mcp/schema.rs)).

#### Every caller names itself

Every direct caller therefore names itself. The gateway mints the header from
the validated session; the evaluation harness and the end-to-end trace check
assert a synthetic principal (`EVALUATION_PRINCIPAL`, default
`evaluation-harness`), because an evaluation run is not a person and should not
be recorded as one. The harness sends no `x-request-id`, so neither agent can
bind an approval to its requests.

## Tool authority

The model proposes tool calls; deterministic software decides which run.

* **NAT:** the callable tools are those NAT is configured with (the MCP
  `include:` list, plus the approval function when enabled), and arguments are
  parsed into models derived from the published schemas.
* **Rig:** every proposed call passes Rig's dispatch hook
  ([`hooks.rs`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/agent/hooks.rs)), which asks one pure function,
  [`ToolPolicy::decide`](https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/guardrails/tools.rs): closed registry,
  JSON-object arguments matching the published schema exactly (unknown fields
  refused, nothing coerced), at most `max_tool_calls` per request, and the
  state-changing tool only into the approval gate. The executors re-check.

In both, the MCP server re-validates every call and remains the authority on
mutations, re-checked against the locked row after the human answers
([APPROVALS.md](APPROVALS.md)).

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
