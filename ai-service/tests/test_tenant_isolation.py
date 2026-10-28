"""Cross-tenant authorization for session reads and mutations."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

PRINCIPALS = {
    "alice-key": {"subject": "alice", "roles": ["owner", "author", "deployer"]},
    "bob-key": {"subject": "bob", "roles": ["author"]},
    "admin-key": {"subject": "admin", "roles": ["admin"]},
}


@pytest.fixture
def tenant_auth(monkeypatch):
    monkeypatch.setenv("PYINTERVIEWBOT_PRINCIPALS", json.dumps(PRINCIPALS))
    return {
        "alice": {"X-API-Key": "alice-key"},
        "bob": {"X-API-Key": "bob-key"},
        "admin": {"X-API-Key": "admin-key"},
    }


def _start(client: TestClient, headers: dict[str, str]) -> dict:
    response = client.post("/sessions", json={}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _bob_requests(session_id: str) -> list[tuple[str, str, dict | None]]:
    return [
        ("GET", f"/sessions/{session_id}/summary", None),
        ("GET", f"/sessions/{session_id}/report", None),
        ("GET", f"/sessions/{session_id}/report/export", None),
        ("GET", f"/sessions/{session_id}/resume-policy", None),
        ("PUT", f"/sessions/{session_id}/resume-policy", {"enabled": False}),
        ("GET", f"/sessions/{session_id}/priorities", None),
        ("POST", f"/sessions/{session_id}/turn", {"transcript": "hello"}),
        ("POST", f"/sessions/{session_id}/resume", None),
        (
            "POST",
            f"/sessions/{session_id}/review",
            {"node": "failure_handling", "reviewer": "bob", "decision": "confirm"},
        ),
    ]


def test_foreign_tenant_gets_404_on_every_session_route(
    client: TestClient, tenant_auth
) -> None:
    session_id = _start(client, tenant_auth["alice"])["session_id"]

    for method, path, payload in _bob_requests(session_id):
        response = client.request(
            method, path, json=payload, headers=tenant_auth["bob"]
        )
        assert response.status_code == 404, (method, path, response.text)

    deleted = client.delete(f"/sessions/{session_id}", headers=tenant_auth["bob"])
    assert deleted.status_code == 404


def test_owner_can_read_and_mutate_own_session(
    client: TestClient, tenant_auth
) -> None:
    session_id = _start(client, tenant_auth["alice"])["session_id"]

    for path in (
        f"/sessions/{session_id}/summary",
        f"/sessions/{session_id}/report",
        f"/sessions/{session_id}/report/export",
        f"/sessions/{session_id}/priorities",
        f"/sessions/{session_id}/resume-policy",
    ):
        response = client.get(path, headers=tenant_auth["alice"])
        assert response.status_code == 200, (path, response.text)

    turn = client.post(
        f"/sessions/{session_id}/turn",
        json={"transcript": "I built a retrying queue."},
        headers=tenant_auth["alice"],
    )
    assert turn.status_code == 200, turn.text


def test_session_lists_are_tenant_scoped(client: TestClient, tenant_auth) -> None:
    session_id = _start(client, tenant_auth["alice"])["session_id"]

    alice = client.get("/sessions", headers=tenant_auth["alice"]).json()
    bob = client.get("/sessions", headers=tenant_auth["bob"]).json()
    admin = client.get("/sessions", headers=tenant_auth["admin"]).json()

    assert [item["session_id"] for item in alice] == [session_id]
    assert bob == []
    assert [item["session_id"] for item in admin] == [session_id]


def test_admin_can_cross_tenant(client: TestClient, tenant_auth) -> None:
    session_id = _start(client, tenant_auth["alice"])["session_id"]

    summary = client.get(
        f"/sessions/{session_id}/summary", headers=tenant_auth["admin"]
    )
    assert summary.status_code == 200, summary.text
    deleted = client.delete(f"/sessions/{session_id}", headers=tenant_auth["admin"])
    assert deleted.status_code == 200, deleted.text


def test_valid_session_token_does_not_override_tenant(
    client: TestClient, tenant_auth
) -> None:
    started = _start(client, tenant_auth["alice"])

    response = client.post(
        f"/sessions/{started['session_id']}/turn",
        json={"transcript": "hello"},
        headers={
            **tenant_auth["bob"],
            "X-Session-Token": started["session_token"],
        },
    )

    assert response.status_code == 404


def test_foreign_tenant_gets_404_on_workflow_routes(
    client: TestClient, tenant_auth
) -> None:
    created = client.post(
        "/workflows",
        json={"name": "Alice workflow", "template_id": "backend_engineer"},
        headers=tenant_auth["alice"],
    )
    assert created.status_code == 200, created.text
    workflow_id = created.json()["workflow_id"]

    for method, path, payload in [
        ("GET", f"/workflows/{workflow_id}", None),
        ("PATCH", f"/workflows/{workflow_id}", {"name": "stolen"}),
        ("GET", f"/workflows/{workflow_id}/audit", None),
        ("GET", f"/workflows/{workflow_id}/deployments", None),
        ("POST", f"/workflows/{workflow_id}/disable", None),
    ]:
        response = client.request(
            method, path, json=payload, headers=tenant_auth["bob"]
        )
        assert response.status_code == 404, (method, path, response.text)


def test_legacy_key_cannot_read_tenant_sessions(
    client: TestClient, tenant_auth, monkeypatch
) -> None:
    session_id = _start(client, tenant_auth["alice"])["session_id"]
    monkeypatch.delenv("PYINTERVIEWBOT_PRINCIPALS")

    assert client.get("/sessions", headers={"X-API-Key": "test-key"}).status_code == 403
    assert (
        client.get(
            f"/sessions/{session_id}/summary", headers={"X-API-Key": "test-key"}
        ).status_code
        == 403
    )
