# SPDX-License-Identifier: Apache-2.0
"""Authenticated NAT FastAPI front-end worker.

NAT's FastAPI front end exposes a supported extension point for exactly this:
``general.front_end.runner_class`` names the ``FastApiFrontEndPluginWorkerBase``
subclass that builds the ASGI application (``nat.front_ends.fastapi.main.get_app``
imports it by dotted path). Subclassing it and wrapping ``build_app`` replaces
the previous build-time rewrite of ``fastapi_front_end_plugin_worker.py``.

Why NAT still authenticates its callers
---------------------------------------
Not publishing NAT's port already makes it unreachable from the browser, and
Docker network membership already limits which containers can route to it.
Neither of those is sufficient here, because NAT *trusts* request headers:
``nat_streaming_react.approval._identity`` reads ``x-authenticated-user-id`` and
``x-request-id`` from the incoming request and binds them into an HMAC-signed,
single-use approval token and the append-only audit trail. Any party able to
open a TCP connection to NAT could therefore mint an approval attributed to an
arbitrary user.

Network reachability answers "can this packet arrive"; it cannot answer "is this
caller the gateway". The static service credential answers the second question,
so the two controls are complementary rather than redundant.

Implementation notes
--------------------
* This is pure ASGI middleware rather than Starlette's ``BaseHTTPMiddleware``.
  ``BaseHTTPMiddleware`` proxies the response through an anyio memory stream,
  which adds a buffering layer to every SSE token; a plain ASGI wrapper passes
  ``send``/``receive`` straight through and cannot affect streaming latency.
* The credential is removed from the ASGI scope after validation, so NAT's
  ``SessionManager`` request metadata, intermediate-step payloads and telemetry
  exporters never observe it.
* Health endpoints stay unauthenticated so container readiness checks work.
* ``GET /version`` reports what this agent is (see ``provenance``) so an
  evaluation run can name the agent it measured. It is authenticated like every
  other non-health route.
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
from nat_streaming_react.observability.trace_context import WorkflowTraceContextMiddleware

logger = logging.getLogger(__name__)

#: Unauthenticated paths. Kept minimal: container/orchestrator liveness only.
PUBLIC_PATHS: frozenset[str] = frozenset({"/health", "/health/live", "/health/ready"})

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


class AuthenticatedFastApiFrontEndPluginWorker(FastApiFrontEndPluginWorker):
    """NAT FastAPI worker that authenticates every non-health request."""

    def build_app(self) -> FastAPI:
        app = super().build_app()

        # Provenance for evaluation artifacts. Registered before the middleware
        # below, so it sits *inside* the authentication layer and is refused
        # without the service credential like any other non-health route.
        @app.get("/version", include_in_schema=False)
        async def version() -> dict[str, Any]:  # pragma: no cover - thin accessor
            return provenance.describe()

        api_key = os.environ.get("NAT_GATEWAY_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "NAT_GATEWAY_API_KEY must be configured: NAT trusts gateway-injected "
                "identity headers, so it must authenticate its callers."
            )

        # Establish one trace context per request before NAT builds any span, so
        # NAT's workflow root and every Guardrails span share a trace. Added
        # first, so it ends up *inside* the authentication layer below: an
        # unauthenticated request never allocates trace state.
        app.add_middleware(
            WorkflowTraceContextMiddleware,
            excluded_paths=PUBLIC_PATHS,
        )

        # add_middleware puts this outermost, ahead of NAT's own middleware, so an
        # unauthenticated request is rejected before NAT processes any of it.
        app.add_middleware(
            StaticServiceKeyMiddleware,
            api_key=api_key,
            public_paths=PUBLIC_PATHS,
        )
        logger.info("Static service-key authentication enabled on all non-health NAT routes")
        return app
