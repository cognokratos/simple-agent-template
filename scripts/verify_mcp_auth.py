#!/usr/bin/env python3
from __future__ import annotations

import os
import urllib.error
import urllib.request

URL = "http://mcp-server:8080/mcp"


def status(request: urllib.request.Request | str) -> int:
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


unauthenticated = status(URL)
if unauthenticated != 401:
    raise SystemExit(f"MCP without API key returned {unauthenticated}, expected 401")

api_key = os.environ.get("MCP_API_KEY", "").strip()
if not api_key:
    raise SystemExit("MCP_API_KEY is missing in the agent container")
authenticated = status(
    urllib.request.Request(
        URL,
        headers={"Authorization": f"Bearer {api_key}"},
    )
)
if authenticated == 401:
    raise SystemExit("MCP rejected the configured API key")

print(f"MCP API-key boundary passed (authenticated endpoint status: {authenticated})")
