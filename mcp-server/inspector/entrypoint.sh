#!/bin/sh
# Render the Inspector server list, then start the Inspector inside a session
# bus with an unlocked keyring so its keychain lookups succeed.
set -eu

CONFIG_TEMPLATE="${INSPECTOR_CONFIG_TEMPLATE:-/config/mcp.template.json}"
CONFIG_PATH="${INSPECTOR_CONFIG_PATH:-/tmp/mcp.json}"

MCP_API_KEY="${MCP_API_KEY:-dev-mcp-api-key-change-me}"
export MCP_API_KEY

if [ -f "$CONFIG_TEMPLATE" ]; then
    envsubst <"$CONFIG_TEMPLATE" >"$CONFIG_PATH"
fi

# The keyring is throwaway: it exists only so `GET /api/servers` can complete.
# Server credentials come from the rendered config file, not from here.
exec dbus-run-session -- sh -c '
    printf "" | gnome-keyring-daemon --unlock --components=secrets >/dev/null 2>&1 || true
    exec mcp-inspector "$@"
' sh "$@"
