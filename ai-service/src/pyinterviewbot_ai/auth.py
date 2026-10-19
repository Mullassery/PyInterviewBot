"""API authentication for the interview service.

Interview transcripts and evidence are sensitive. The service now requires an
API key for every non-health endpoint when ``PYINTERVIEWBOT_API_KEY`` is set
(via ``X-API-Key`` or ``Authorization: Bearer``). When it is not set, only
loopback clients are allowed so a default localhost install still works while
a network-exposed service fails closed.
"""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException, Request

API_KEY_ENV = "PYINTERVIEWBOT_API_KEY"


def _loopback(host: str | None) -> bool:
    if not host:
        return False
    return host in {"127.0.0.1", "::1", "localhost"} or host.startswith("127.")


async def require_auth(
    request: Request,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> None:
    expected = os.environ.get(API_KEY_ENV)
    if expected:
        supplied = x_api_key
        if (
            not supplied
            and authorization
            and authorization.lower().startswith("bearer ")
        ):
            supplied = authorization[7:].strip()
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="invalid or missing API key")
        return
    client = request.client.host if request.client else None
    if not _loopback(client):
        raise HTTPException(
            status_code=403,
            detail=(
                "refusing non-loopback request without authentication; set "
                f"{API_KEY_ENV} and send X-API-Key"
            ),
        )


__all__ = ["API_KEY_ENV", "require_auth"]
