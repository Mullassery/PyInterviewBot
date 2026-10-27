"""API key/loopback enforcement tests using a minimal FastAPI app."""

from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

pytest.importorskip("fastapi")

from pyinterviewbot_ai.auth import Principal, require_auth

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/protected", dependencies=[Depends(require_auth)])
def protected():
    return {"ok": True}


@app.get("/whoami")
def whoami(principal: Annotated[Principal, Depends(require_auth)]):
    return {
        "subject": principal.subject,
        "roles": sorted(principal.roles),
    }


def test_health_needs_no_auth(monkeypatch):
    monkeypatch.delenv("PYINTERVIEWBOT_API_KEY", raising=False)
    assert TestClient(app).get("/health").status_code == 200


def test_non_loopback_without_key_is_rejected(monkeypatch):
    monkeypatch.delenv("PYINTERVIEWBOT_API_KEY", raising=False)
    assert TestClient(app).post("/protected").status_code == 403


def test_api_key_is_enforced(monkeypatch):
    monkeypatch.setenv("PYINTERVIEWBOT_API_KEY", "secret")
    client = TestClient(app)
    assert client.post("/protected").status_code == 401
    assert client.post("/protected", headers={"X-API-Key": "secret"}).status_code == 200


def test_bearer_key_is_accepted(monkeypatch):
    monkeypatch.setenv("PYINTERVIEWBOT_API_KEY", "secret")
    client = TestClient(app)
    assert (
        client.post(
            "/protected", headers={"Authorization": "Bearer secret"}
        ).status_code
        == 200
    )


def test_legacy_api_key_gets_operator_identity(monkeypatch):
    monkeypatch.setenv("PYINTERVIEWBOT_API_KEY", "secret")
    body = TestClient(app).get(
        "/whoami", headers={"X-API-Key": "secret"}
    ).json()
    assert body == {
        "subject": "local-operator",
        "roles": ["approver", "author", "deployer", "owner"],
    }


def test_principal_map_assigns_subject_and_roles(monkeypatch):
    monkeypatch.setenv("PYINTERVIEWBOT_API_KEY", "secret")
    monkeypatch.setenv(
        "PYINTERVIEWBOT_PRINCIPALS",
        '{"alice-key": {"subject": "alice", "roles": ["owner", "author"]}}',
    )
    body = TestClient(app).get(
        "/whoami", headers={"X-API-Key": "alice-key"}
    ).json()
    assert body == {"subject": "alice", "roles": ["author", "owner"]}


async def test_loopback_without_configured_key_gets_loopback_identity(monkeypatch):
    from starlette.requests import Request

    monkeypatch.delenv("PYINTERVIEWBOT_API_KEY", raising=False)
    monkeypatch.delenv("PYINTERVIEWBOT_PRINCIPALS", raising=False)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/whoami",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )
    principal = await require_auth(request)
    assert principal.subject == "local-loopback"
    assert principal.roles == frozenset({"owner", "author", "approver", "deployer"})


def test_unknown_principal_is_rejected(monkeypatch):
    monkeypatch.setenv("PYINTERVIEWBOT_API_KEY", "secret")
    monkeypatch.setenv(
        "PYINTERVIEWBOT_PRINCIPALS",
        '{"alice-key": {"subject": "alice", "roles": ["owner"]}}',
    )
    assert (
        TestClient(app).get(
            "/whoami", headers={"X-API-Key": "unknown"}
        ).status_code
        == 401
    )
