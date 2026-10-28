"""API authentication for the interview service.

Interview transcripts and evidence are sensitive. The service now requires an
API key for every non-health endpoint when ``PYINTERVIEWBOT_API_KEY`` is set
(via ``X-API-Key`` or ``Authorization: Bearer``). When it is not set, only
loopback clients are allowed so a default localhost install still works while
a network-exposed service fails closed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request

API_KEY_ENV = "PYINTERVIEWBOT_API_KEY"
PRINCIPALS_ENV = "PYINTERVIEWBOT_PRINCIPALS"
DEFAULT_ROLES = frozenset({"owner", "author", "approver", "deployer"})
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    subject: str
    roles: frozenset[str] = DEFAULT_ROLES
    source: str = "principal"

    def has_role(self, role: str) -> bool:
        return role in self.roles or "admin" in self.roles


def _loopback(host: str | None) -> bool:
    if not host:
        return False
    return host in {"127.0.0.1", "::1", "localhost"} or host.startswith("127.")


def _supplied_key(x_api_key: str | None, authorization: str | None) -> str | None:
    supplied = x_api_key
    if not supplied and authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    return supplied


def _configured_principals() -> dict[str, Principal]:
    raw = os.environ.get(PRINCIPALS_ENV, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("ignoring invalid %s JSON", PRINCIPALS_ENV)
        return {}
    if not isinstance(parsed, dict):
        logger.error("ignoring invalid %s value; expected an object", PRINCIPALS_ENV)
        return {}
    principals: dict[str, Principal] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        subject = value.get("subject")
        roles = value.get("roles", [])
        if not isinstance(subject, str) or not subject or not isinstance(roles, list):
            continue
        principals[key] = Principal(
            subject=subject,
            roles=frozenset(role for role in roles if isinstance(role, str) and role),
            source="principal",
        )
    return principals


def _match_principal(supplied: str) -> Principal | None:
    for key, principal in _configured_principals().items():
        if hmac.compare_digest(supplied, key):
            return principal
    expected = os.environ.get(API_KEY_ENV)
    if expected and hmac.compare_digest(supplied, expected):
        subject = os.environ.get("PYINTERVIEWBOT_API_KEY_SUBJECT", "local-operator")
        raw_roles = os.environ.get("PYINTERVIEWBOT_API_KEY_ROLES", "")
        roles = (
            frozenset(role.strip() for role in raw_roles.split(",") if role.strip())
            if raw_roles
            else DEFAULT_ROLES
        )
        return Principal(subject=subject, roles=roles, source="legacy")
    return None


async def require_auth(
    request: Request,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> Principal:
    configured = bool(os.environ.get(API_KEY_ENV)) or bool(
        os.environ.get(PRINCIPALS_ENV)
    )
    if configured:
        supplied = _supplied_key(x_api_key, authorization)
        if not supplied:
            raise HTTPException(status_code=401, detail="invalid or missing API key")
        principal = _match_principal(supplied)
        if principal is None:
            raise HTTPException(status_code=401, detail="invalid or missing API key")
        return principal
    client = request.client.host if request.client else None
    if not _loopback(client):
        raise HTTPException(
            status_code=403,
            detail=(
                "refusing non-loopback request without authentication; set "
                f"{API_KEY_ENV} and send X-API-Key"
            ),
        )
    return Principal(subject="local-loopback", roles=DEFAULT_ROLES, source="loopback")


async def require_tenant_auth(
    principal: Annotated[Principal, Depends(require_auth)],
) -> Principal:
    if principal.source == "legacy":
        raise HTTPException(
            status_code=403,
            detail=(
                "tenant identity required; configure "
                "PYINTERVIEWBOT_PRINCIPALS and use a principal key"
            ),
        )
    return principal


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_token_matches(stored_hash: str, token: str | None) -> bool:
    if not stored_hash:
        return False
    if not token:
        return False
    return hmac.compare_digest(hash_session_token(token), stored_hash)


def session_token_required() -> bool:
    return os.environ.get("PYINTERVIEWBOT_REQUIRE_SESSION_TOKEN", "").lower() in {
        "1",
        "true",
        "yes",
    }


__all__ = [
    "API_KEY_ENV",
    "DEFAULT_ROLES",
    "PRINCIPALS_ENV",
    "Principal",
    "hash_session_token",
    "require_auth",
    "require_tenant_auth",
    "session_token_matches",
    "session_token_required",
]
