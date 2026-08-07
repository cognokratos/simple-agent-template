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
    output = Path(os.environ.get("KEYCLOAK_REALM_OUTPUT", "/import/alerts-realm.json"))
    realm_name = os.environ.get("KEYCLOAK_REALM", "alerts")
    client_id = os.environ.get("KEYCLOAK_GATEWAY_CLIENT_ID", "alerts-gateway")
    client_secret = required("KEYCLOAK_GATEWAY_CLIENT_SECRET")
    analyst_username = os.environ.get("KEYCLOAK_ANALYST_USERNAME", "analyst")
    analyst_password = required("KEYCLOAK_ANALYST_PASSWORD")
    analyst_email = os.environ.get("KEYCLOAK_ANALYST_EMAIL", "analyst@example.test")
    ui_public_url = os.environ.get("UI_PUBLIC_URL", "http://localhost:3000").rstrip("/")
    oidc_callback_url = os.environ.get(
        "OIDC_CALLBACK_URL",
        f"{ui_public_url}/api/gateway/auth/callback",
    )

    realm = {
        "realm": realm_name,
        "enabled": True,
        "displayName": "Alerts Agent",
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
                    "name": "analyst",
                    "description": "May use the alert investigation agent",
                    "composite": False,
                    "clientRole": False,
                }
            ]
        },
        "clients": [
            {
                "clientId": client_id,
                "name": "Alerts Rust authentication gateway",
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
                "username": analyst_username,
                "enabled": True,
                "emailVerified": True,
                "email": analyst_email,
                "firstName": "Demo",
                "lastName": "Analyst",
                "realmRoles": ["analyst"],
                "credentials": [
                    {
                        "type": "password",
                        "value": analyst_password,
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
