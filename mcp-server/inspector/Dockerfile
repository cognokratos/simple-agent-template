# MCP Inspector with a working Secret Service.
#
# The upstream image ships no keyring. Inspector 2.x reads per-server secrets
# from the OS keychain on every `GET /api/servers`, and the native keyring
# constructor throws `PermissionDenied` when no Secret Service is reachable, so
# the request 500s and the web UI renders "No servers configured".
FROM ghcr.io/modelcontextprotocol/inspector:2.1.0

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        dbus-x11 \
        gettext-base \
        gnome-keyring \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/inspector-entrypoint.sh
RUN chmod +x /usr/local/bin/inspector-entrypoint.sh

USER node

ENTRYPOINT ["/usr/local/bin/inspector-entrypoint.sh"]
CMD ["--web"]
