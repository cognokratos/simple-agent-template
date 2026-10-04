# SPDX-License-Identifier: Apache-2.0
"""Authenticated NAT FastAPI front-end worker.

NAT's FastAPI front end exposes a configuration extension point for exactly
this: ``general.front_end.runner_class`` names the
``FastApiFrontEndPluginWorkerBase`` subclass that builds the ASGI application
(``nat.front_ends.fastapi.main.get_app`` imports it by dotted path). Subclassing
it and wrapping ``build_app`` replaces the previous build-time rewrite of
``fastapi_front_end_plugin_worker.py`` in site-packages.

Why NAT still authenticates its callers
---------------------------------------
Not publishing NAT's port already makes it unreachable from the browser, and
Docker network membership already limits which containers can route to it.
Neither of those is sufficient, because NAT *trusts* request headers: the
gateway injects ``x-authenticated-user-id`` and friends, and downstream code
binds that identity into audit records and (when the optional approval feature
is enabled) into signed approval tokens. Any party able to open a TCP connection
to NAT could therefore act as an arbitrary user.

Network reachability answers "can this packet arrive"; it cannot answer "is this
caller the gateway". The static service credential answers the second question,
so the two controls are complementary rather than redundant.

A third question — "who is this caller acting for" — is answered by
``RequireIdentityHeaderMiddleware``. NAT 1.9 can resolve that header itself
(``general.front_end.identity_header``), and this deployment configures it so
``Context.user_id`` is populated, but its refusal does not reach the client on
the workflow routes. See that class for the measured behaviour.

Implementation notes
--------------------
* This is pure ASGI middleware rather than Starlette's ``BaseHTTPMiddleware``.
  ``BaseHTTPMiddleware`` proxies the response through an anyio memory stream,
  which adds a buffering layer to every SSE token; a plain ASGI wrapper passes
  ``send``/``receive`` straight through and cannot affect streaming latency.
* The credential is compared in constant time and then removed from the ASGI
  scope, so NAT's ``SessionManager`` request metadata, intermediate-step
  payloads and telemetry exporters never observe it.
* Health endpoints stay unauthenticated so container readiness checks work.
  ``PUBLIC_PATHS`` is deliberately minimal — liveness only.
* ``GET /version`` reports what this agent is (see ``provenance``) so an
  evaluation run can name the agent it measured. It is authenticated like every
  other non-health route — credential *and* asserted identity — and reports
  digests rather than prompt text or any credential.
"""

import hmac
import logging
import os
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from nat.front_ends.fastapi.fastapi_front_end_plugin_worker import FastApiFrontEndPluginWorker

from nat_streaming_react import provenance
from nat_streaming_react.interaction_guard import IDENTITY_HEADER
from nat_streaming_react.interaction_guard import OwnerAwareExecutionStore
from nat_streaming_react.interaction_guard import ResponderIdentityMiddleware
from nat_streaming_react.interaction_guard import _sole_identity_header
from nat_streaming_react.observability.trace_context import WorkflowTraceContextMiddleware

logger = logging.getLogger(__name__)

#: Unauthenticated paths. Kept minimal: container/orchestrator liveness only.
PUBLIC_PATHS: frozenset[str] = frozenset({"/health", "/health/live", "/health/ready"})

#: Environment variable holding the gateway-to-NAT service credential.
API_KEY_ENV = "NAT_GATEWAY_API_KEY"

_UNAUTHORIZED_BODY = b'{"error":"invalid or missing internal API key"}'


class StaticServiceKeyMiddleware:
    """Require a static bearer credential on every non-health NAT route."""

    def __init__(
        self,
        app: Any,
        *,
        api_key: str,
        public_paths: frozenset[str] = PUBLIC_PATHS,
    ) -> None:
        self.app = app
        self._expected = f"Bearer {api_key}".encode("utf-8")
        self._public_paths = public_paths

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[Any]],
        send: Callable[[Any], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http" or scope.get("path") in self._public_paths:
            await self.app(scope, receive, send)
            return

        provided = b""
        sanitized: list[tuple[bytes, bytes]] = []
        for name, value in scope.get("headers", ()):
            if name.lower() == b"authorization":
                provided = value
            else:
                sanitized.append((name, value))

        # compare_digest, not ==: a plain comparison leaks the length of the
        # matching prefix through timing, which is enough to recover a key.
        if not hmac.compare_digest(provided, self._expected):
            logger.warning(
                "Rejected unauthenticated %s %s from %s",
                scope.get("method"),
                scope.get("path"),
                (scope.get("client") or ("unknown", 0))[0],
            )
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                ],
            })
            await send({"type": "http.response.body", "body": _UNAUTHORIZED_BODY})
            return

        # Validated: drop the service credential before any NAT component,
        # session metadata store or telemetry exporter can observe it.
        await self.app({**scope, "headers": sanitized}, receive, send)


_MISSING_IDENTITY_BODY = b'{"error":"missing or ambiguous authenticated identity"}'


class RequireIdentityHeaderMiddleware:
    """Require exactly one non-empty identity header on every non-health route.

    Why this exists when ``identity_header`` is already configured
    -------------------------------------------------------------
    ``general.front_end.identity_header`` makes NAT 1.9 resolve the
    gateway-asserted identity into ``Context.user_id``, and ``UserManager``
    raises ``IdentityHeaderError`` when it is missing, empty or repeated. NAT
    registers an exception handler that turns that into a 401.

    On this deployment's endpoints that handler is never reached. NAT's
    ``add_generate_routes`` passes ``enable_interactive=True`` **unconditionally**
    for the workflow path and its ``/stream`` and ``/full`` variants — the
    ``enable_interactive_extensions`` setting only governs whether the
    ``/executions/...`` endpoints are mounted, not which runner serves the
    workflow. The interactive runner acquires the session inside a background
    task, after the response has begun, and wraps it in a blanket
    ``except Exception`` that pushes the error into the stream body. So a request
    with no asserted identity is answered **200** with a ``WORKFLOW_ERROR``
    event, not 401, and the identity requirement silently becomes advisory.

    Measured, not assumed: without this middleware, a keyed request carrying no
    identity header returns 200 while the agent log shows
    ``IdentityHeaderError: Configured identity header 'x-authenticated-user-id'
    is missing``. ``make auth-test`` asserts the 401 this middleware produces.

    The configured ``identity_header`` is still worth keeping — it is what
    populates ``Context.user_id``, and therefore the per-user span attribution
    governed by ``observability.trace_processor.UserIdentityProcessor``. The two
    are complementary: NAT resolves the identity, this decides whether a request
    without one is served at all.

    Pure ASGI for the same reason as the other middleware here: it must not
    interpose a buffering layer on a streamed response.
    """

    def __init__(
        self,
        app: Any,
        *,
        public_paths: frozenset[str] = PUBLIC_PATHS,
    ) -> None:
        self.app = app
        self._public_paths = public_paths

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[Any]],
        send: Callable[[Any], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http" or scope.get("path") in self._public_paths:
            await self.app(scope, receive, send)
            return

        # One parsing rule for the whole package: exactly one non-empty
        # occurrence, matching both NAT's UserManager and the approval
        # responder check, so no two layers disagree about who is asking.
        if _sole_identity_header(scope) is None:
            logger.warning(
                "Rejected %s %s: no unambiguous %s",
                scope.get("method"),
                scope.get("path"),
                IDENTITY_HEADER,
            )
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({"type": "http.response.body", "body": _MISSING_IDENTITY_BODY})
            return

        await self.app(scope, receive, send)


class AuthenticatedFastApiFrontEndPluginWorker(FastApiFrontEndPluginWorker):
    """NAT FastAPI worker that authenticates every non-health request."""

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        # NAT's interaction-response endpoint authorizes on knowledge of two
        # UUIDs and nothing else. Substituting the store is the supported way to
        # add an owner and offered-choice check without touching NAT: its routes
        # close over whatever this attribute holds. See interaction_guard.
        self._execution_store = OwnerAwareExecutionStore()

    def build_app(self) -> FastAPI:
        app = super().build_app()

        # Provenance for evaluation artifacts. Registered before the middleware
        # below, so it sits *inside* the authentication layer and is refused
        # without the service credential like any other non-health route.
        @app.get("/version", include_in_schema=False)
        async def version() -> dict[str, Any]:  # pragma: no cover - thin accessor
            return provenance.describe()

        api_key = os.environ.get(API_KEY_ENV, "").strip()
        if not api_key:
            raise RuntimeError(
                f"{API_KEY_ENV} must be configured: NAT trusts gateway-injected "
                "identity headers, so it must authenticate its callers."
            )

        # Record the authenticated caller for the duration of the request, so
        # the execution store can check that an approval response comes from the
        # user the prompt was addressed to. Added before the layers below, so it
        # ends up innermost and only ever sees authenticated requests.
        app.add_middleware(ResponderIdentityMiddleware)

        # Establish one trace context per request before NAT builds any span, so
        # NAT's workflow root and every Guardrails span share a trace. Added
        # first, so it ends up *inside* the authentication layer below: an
        # unauthenticated request never allocates trace state.
        app.add_middleware(
            WorkflowTraceContextMiddleware,
            excluded_paths=PUBLIC_PATHS,
        )

        # Just inside the credential check below, so the two 401s stay ordered:
        # "you are not the gateway" is answered before "you did not say who you
        # are acting for". NAT's own identity_header enforcement cannot do this
        # on the workflow routes; see RequireIdentityHeaderMiddleware.
        app.add_middleware(
            RequireIdentityHeaderMiddleware,
            public_paths=PUBLIC_PATHS,
        )

        # add_middleware puts this outermost, ahead of NAT's own middleware, so
        # an unauthenticated request is rejected before NAT processes any of it.
        app.add_middleware(
            StaticServiceKeyMiddleware,
            api_key=api_key,
            public_paths=PUBLIC_PATHS,
        )
        logger.info(
            "Static service-key authentication enabled on all non-health NAT routes"
        )
        return app
