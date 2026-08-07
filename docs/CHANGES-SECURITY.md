# Security extension

Added:

- Keycloak with a generated local-development realm, confidential client,
  Authorization Code flow, PKCE S256, analyst role, and seeded analyst user;
- a Rust OIDC BFF gateway with server-side sessions, ID-token validation,
  refresh, logout, CSRF protection, strict route/request allowlists, SSE
  proxying, and trusted identity propagation;
- assistant-ui proxy routes under `/api/gateway`, including the OIDC callback,
  so the Rust gateway remains Compose-internal in normal operation;
- browser cookies scoped to `/api/gateway` rather than the broad `/api` path;
- static bearer authentication from gateway/evaluator to NAT;
- static bearer authentication from NAT to the Rust MCP server;
- internal-only gateway, NAT, and MCP ports in normal Compose, with a
  loopback-only diagnostic override;
- `make auth-test`, `make security-test`, `make reset-auth`, gateway/Keycloak
  logs, and gateway rebuild targets;
- a root `../.gitignore` that excludes `../.env`, Rust/Next build output, and local caches;
- security documentation and static topology validation.
