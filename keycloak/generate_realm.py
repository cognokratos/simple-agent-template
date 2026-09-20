#!/usr/bin/env python3
"""Generate the local-development Keycloak realm from environment secrets."""

from __future__ import annotations

import json
import os
from pathlib import Path


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} must be configured")
    return value


def main() -> None:
    # Keycloak requires the imported file's name to match its `realm` field
    # (see --import-realm), so this must stay in step with the "tickets" realm
    # name kept below as a stable, unchanged auth identifier.
    output = Path(os.environ.get("KEYCLOAK_REALM_OUTPUT", "/import/tickets-realm.json"))
    realm_name = os.environ.get("KEYCLOAK_REALM", "tickets")
    client_id = os.environ.get("KEYCLOAK_GATEWAY_CLIENT_ID", "tickets-gateway")
    client_secret = required("KEYCLOAK_GATEWAY_CLIENT_SECRET")
    agent_username = os.environ.get("KEYCLOAK_AGENT_USERNAME", "agent")
    agent_password = required("KEYCLOAK_AGENT_PASSWORD")
    agent_email = os.environ.get("KEYCLOAK_AGENT_EMAIL", "agent@example.test")
    ui_public_url = os.environ.get("UI_PUBLIC_URL", "http://localhost:3000").rstrip("/")
    oidc_callback_url = os.environ.get(
        "OIDC_CALLBACK_URL",
        f"{ui_public_url}/api/gateway/auth/callback",
    )

    realm = {
        "realm": realm_name,
        "enabled": True,
        "displayName": "Support Assistant",
        "sslRequired": "external",
        "registrationAllowed": False,
        "resetPasswordAllowed": True,
        "rememberMe": True,
        "loginWithEmailAllowed": True,
        "duplicateEmailsAllowed": False,
        "bruteForceProtected": True,
        "accessTokenLifespan": 300,
        "ssoSessionIdleTimeout": 1800,
        "ssoSessionMaxLifespan": 36000,
        "roles": {
            "realm": [
                {
                    "name": "agent",
                    "description": "May use the support ticket triage assistant",
                    "composite": False,
                    "clientRole": False,
                }
            ]
        },
        "clients": [
            {
                "clientId": client_id,
                "name": "Support Assistant Rust authentication gateway",
                "enabled": True,
                "protocol": "openid-connect",
                "clientAuthenticatorType": "client-secret",
                "secret": client_secret,
                "publicClient": False,
                "bearerOnly": False,
                "consentRequired": False,
                "standardFlowEnabled": True,
                "implicitFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": False,
                "frontchannelLogout": True,
                "fullScopeAllowed": True,
                "rootUrl": ui_public_url,
                "baseUrl": ui_public_url,
                "redirectUris": [oidc_callback_url],
                "webOrigins": [ui_public_url],
                "defaultClientScopes": ["web-origins", "acr", "profile", "roles", "email"],
                "optionalClientScopes": ["address", "phone", "offline_access", "microprofile-jwt"],
                "attributes": {
                    "pkce.code.challenge.method": "S256",
                    "post.logout.redirect.uris": f"{ui_public_url}/*",
                    "oauth2.device.authorization.grant.enabled": "false",
                    "oidc.ciba.grant.enabled": "false",
                },
                "protocolMappers": [
                    {
                        "name": "realm roles in userinfo",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-usermodel-realm-role-mapper",
                        "consentRequired": False,
                        "config": {
                            "multivalued": "true",
                            "userinfo.token.claim": "true",
                            "id.token.claim": "true",
                            "access.token.claim": "true",
                            "claim.name": "realm_access.roles",
                            "jsonType.label": "String",
                        },
                    }
                ],
            }
        ],
        "users": [
            {
                "username": agent_username,
                "enabled": True,
                "emailVerified": True,
                "email": agent_email,
                "firstName": "Demo",
                "lastName": "Agent",
                "realmRoles": ["agent"],
                "credentials": [
                    {
                        "type": "password",
                        "value": agent_password,
                        "temporary": False,
                    }
                ],
            }
        ],
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(realm, indent=2) + "\n", encoding="utf-8")
    print(f"Generated Keycloak realm at {output}")


if __name__ == "__main__":
    main()
