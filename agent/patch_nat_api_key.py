#!/usr/bin/env python3
"""Require a static bearer key on NAT's FastAPI routes except /health."""

from __future__ import annotations

import importlib.util
from pathlib import Path

MARKER = "NAT_STATIC_API_KEY_SECURITY_PATCH"


def locate_worker() -> Path:
    spec = importlib.util.find_spec("nat.front_ends.fastapi.fastapi_front_end_plugin_worker")
    if spec is None or spec.origin is None:
        raise SystemExit("Could not locate NAT FastAPI front-end worker")
    return Path(spec.origin)


def main() -> None:
    path = locate_worker()
    source = path.read_text(encoding="utf-8")
    if MARKER in source:
        print(f"NAT API-key patch already present: {path}")
        return

    source = source.replace("import asyncio\n", "import asyncio\nimport hmac\n", 1)
    source = source.replace(
        "from fastapi import Response\n",
        "from fastapi import Response\nfrom fastapi.responses import JSONResponse\n",
        1,
    )

    needle = "        nat_app = FastAPI(lifespan=lifespan)\n"
    replacement = '''        nat_app = FastAPI(lifespan=lifespan)

        # NAT_STATIC_API_KEY_SECURITY_PATCH
        # Only the Rust gateway and the internal evaluator know this key. The
        # health endpoint remains unauthenticated for container readiness checks.
        static_api_key = os.environ.get("NAT_GATEWAY_API_KEY", "").strip()
        if not static_api_key:
            raise RuntimeError("NAT_GATEWAY_API_KEY must be configured")
        expected_authorization = f"Bearer {static_api_key}"
        public_paths = {"/health", "/health/live", "/health/ready"}

        @nat_app.middleware("http")
        async def static_api_key_filter(request: Request, call_next: Callable[[Request], Awaitable[Response]]):
            if request.url.path in public_paths:
                return await call_next(request)

            # Read from the raw ASGI scope rather than request.headers so the
            # Headers cache is not populated before the secret is removed.
            provided = ""
            sanitized_headers = []
            for name, value in request.scope.get("headers", []):
                if name.lower() == b"authorization":
                    provided = value.decode("latin-1")
                else:
                    sanitized_headers.append((name, value))

            if not hmac.compare_digest(provided.encode("utf-8"), expected_authorization.encode("utf-8")):
                return JSONResponse(
                    status_code=401,
                    content={"error": "invalid or missing internal API key"},
                    headers={"WWW-Authenticate": "Bearer"},
                )

            # Do not let SessionManager request metadata or telemetry exporters
            # capture the static service credential after it has been validated.
            request.scope["headers"] = sanitized_headers
            return await call_next(request)
'''
    if needle not in source:
        raise SystemExit(f"NAT FastAPI patch anchor was not found in {path}")
    source = source.replace(needle, replacement, 1)
    path.write_text(source, encoding="utf-8")
    print(f"Applied NAT API-key security patch: {path}")


if __name__ == "__main__":
    main()
