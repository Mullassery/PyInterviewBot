"""API key/loopback enforcement tests using a minimal FastAPI app."""

from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

pytest.importorskip("fastapi")

from pyinterviewbot_ai.auth import Principal, require_auth, require_tenant_auth

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/protected", dependencies=[Depends(require_auth)])
def protected():
    return {"ok": True}


@app.get("/tenant")
def tenant(principal: Annotated[Principal, Depends(require_tenant_auth)]):
    return {"subject": principal.subject, "source": principal.source}


@app.get("/whoami")
def whoami(principal: Annotated[Principal, Depends(require_auth)]):
    return {
        "subject": principal.subject,
        "roles": sorted(principal.roles),
        "source": principal.source,
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
    monkeypatch.delenv("PYINTERVIEWBOT_PRINCIPALS", raising=False)
    client = TestClient(app)
    body = client.get("/whoami", headers={"X-API-Key": "secret"}).json()
    assert body == {
        "subject": "local-operator",
        "roles": ["approver", "author", "deployer", "owner"],
        "source": "legacy",
    }


def test_principal_map_assigns_subject_and_roles(monkeypatch):
    monkeypatch.setenv("PYINTERVIEWBOT_API_KEY", "secret")
    monkeypatch.setenv(
        "PYINTERVIEWBOT_PRINCIPALS",
        '{"alice-key": {"subject": "alice", "roles": ["owner", "author"]}}',
    )
    client = TestClient(app)
    body = client.get("/whoami", headers={"X-API-Key": "alice-key"}).json()
    assert body == {
        "subject": "alice",
        "roles": ["author", "owner"],
        "source": "principal",
    }
    assert (
        client.get("/tenant", headers={"X-API-Key": "alice-key"}).status_code
        == 200
    )


def test_legacy_key_is_not_a_tenant(monkeypatch):
    monkeypatch.setenv("PYINTERVIEWBOT_API_KEY", "secret")
    monkeypatch.delenv("PYINTERVIEWBOT_PRINCIPALS", raising=False)
    response = TestClient(app).get("/tenant", headers={"X-API-Key": "secret"})
    assert response.status_code == 403
    assert "PYINTERVIEWBOT_PRINCIPALS" in response.json()["detail"]


def test_principal_key_is_a_tenant(monkeypatch):
    monkeypatch.setenv("PYINTERVIEWBOT_API_KEY", "secret")
    monkeypatch.setenv(
        "PYINTERVIEWBOT_PRINCIPALS",
        '{"alice-key": {"subject": "alice", "roles": ["author"]}}',
    )
    response = TestClient(app).get("/tenant", headers={"X-API-Key": "alice-key"})
    assert response.status_code == 200
    assert response.json()["subject"] == "alice"


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
    assert principal.source == "loopback"


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
