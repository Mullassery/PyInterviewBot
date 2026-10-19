"""API key/loopback enforcement tests using a minimal FastAPI app."""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

pytest.importorskip("fastapi")

from pyinterviewbot_ai.auth import require_auth

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/protected", dependencies=[Depends(require_auth)])
def protected():
    return {"ok": True}


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
