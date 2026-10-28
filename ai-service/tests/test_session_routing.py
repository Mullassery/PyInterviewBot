from __future__ import annotations

import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pyinterviewbot_ai import db
from pyinterviewbot_ai import workflow_routing as routing

PRINCIPALS = {
    "alice-key": {
        "subject": "alice",
        "roles": ["owner", "author", "deployer"],
    },
    "bob-key": {"subject": "bob", "roles": ["approver"]},
}


@pytest.fixture
def workflow_auth(monkeypatch):
    monkeypatch.setenv("PYINTERVIEWBOT_PRINCIPALS", json.dumps(PRINCIPALS))
    monkeypatch.setenv("PYINTERVIEWBOT_PRODUCTION_BINDING_REVISION", "prod-test-1")
    monkeypatch.setenv("PYINTERVIEWBOT_LLM_PROVIDER", "mock")
    monkeypatch.setenv("PYINTERVIEWBOT_ASR_PROVIDER", "mock")
    monkeypatch.setenv("PYINTERVIEWBOT_TTS_PROVIDER", "mock")
    return {
        "alice": {"X-API-Key": "alice-key"},
        "bob": {"X-API-Key": "bob-key"},
    }


def _create_workflow(
    client: TestClient,
    headers: dict[str, str],
    *,
    template_id: str = "backend_engineer",
    name: str = "Session routing",
) -> str:
    response = client.post(
        "/workflows",
        json={
            "name": name,
            "description": "Pinned session route.",
            "template_id": template_id,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["workflow_id"]


def _publish(
    client: TestClient, headers: dict[str, str], workflow_id: str, key: str
) -> dict:
    response = client.post(
        f"/workflows/{workflow_id}/versions",
        headers={**headers, "Idempotency-Key": f"version-{key}"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _fully_promote(
    client: TestClient,
    workflow_auth: dict[str, dict[str, str]],
    workflow_id: str,
    *,
    key: str = "v1",
) -> dict:
    version = _publish(client, workflow_auth["alice"], workflow_id, key)
    sandbox = client.post(
        f"/workflows/{workflow_id}/versions/{version['version']}/sandbox/deploy",
        headers={
            **workflow_auth["alice"],
            "Idempotency-Key": f"sandbox-{key}",
        },
    )
    assert sandbox.status_code == 200, sandbox.text
    test_run = client.post(
        f"/workflows/{workflow_id}/versions/{version['version']}/tests",
        json={"deployment_id": sandbox.json()["deployment_id"]},
        headers={
            **workflow_auth["alice"],
            "Idempotency-Key": f"tests-{key}",
        },
    )
    assert test_run.status_code == 200, test_run.text
    requested = client.post(
        f"/workflows/{workflow_id}/versions/{version['version']}/approvals",
        json={
            "test_run_id": test_run.json()["test_run_id"],
            "approver_subject": "bob",
            "note": "Review sandbox evidence.",
        },
        headers={
            **workflow_auth["alice"],
            "Idempotency-Key": f"approval-{key}",
        },
    )
    assert requested.status_code == 200, requested.text
    approved = client.post(
        f"/workflows/{workflow_id}/versions/{version['version']}/approvals/"
        f"{requested.json()['approval_id']}/decision",
        json={"decision": "approve", "note": "Approved."},
        headers=workflow_auth["bob"],
    )
    assert approved.status_code == 200, approved.text
    production = client.post(
        f"/workflows/{workflow_id}/versions/{version['version']}/production/deploy",
        json={
            "test_run_id": test_run.json()["test_run_id"],
            "approval_id": requested.json()["approval_id"],
        },
        headers={
            **workflow_auth["alice"],
            "Idempotency-Key": f"production-{key}",
        },
    )
    assert production.status_code == 200, production.text
    return production.json()


def _start_routed_session(
    client: TestClient,
    workflow_auth: dict[str, dict[str, str]],
    workflow_id: str,
    *,
    template_id: str = "rag_architect",
) -> dict:
    response = client.post(
        "/sessions",
        json={"template_id": template_id, "workflow_id": workflow_id},
        headers=workflow_auth["alice"],
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _replace_template_id(session_id: str, template_id: str) -> None:
    async with db.connect() as conn:
        await conn.execute(
            "UPDATE sessions SET template_id = ? WHERE id = ?",
            (template_id, session_id),
        )
        await conn.commit()


def test_start_session_routes_to_active_workflow(client, workflow_auth) -> None:
    workflow_id = _create_workflow(client, workflow_auth["alice"])
    deployment = _fully_promote(client, workflow_auth, workflow_id)

    body = _start_routed_session(client, workflow_auth, workflow_id)

    assert body["template_id"] == "backend_engineer"
    assert body["workflow_id"] == workflow_id
    assert body["workflow_version"] == 1
    assert body["workflow_deployment_id"] == deployment["deployment_id"]


def test_legacy_key_cannot_create_tenant_session(
    client, workflow_auth, monkeypatch
) -> None:
    monkeypatch.delenv("PYINTERVIEWBOT_PRINCIPALS")

    response = client.post(
        "/sessions",
        json={"template_id": "backend_engineer"},
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 403
    assert "PYINTERVIEWBOT_PRINCIPALS" in response.json()["detail"]


def test_start_session_unknown_workflow_is_404(client, workflow_auth) -> None:
    response = client.post(
        "/sessions",
        json={"workflow_id": str(uuid4())},
        headers=workflow_auth["alice"],
    )

    assert response.status_code == 404


def test_start_session_stale_binding_is_409(
    client, workflow_auth, monkeypatch
) -> None:
    workflow_id = _create_workflow(client, workflow_auth["alice"])
    _fully_promote(client, workflow_auth, workflow_id)
    monkeypatch.setenv("PYINTERVIEWBOT_PRODUCTION_BINDING_REVISION", "prod-test-2")

    response = client.post(
        "/sessions",
        json={"workflow_id": workflow_id},
        headers=workflow_auth["alice"],
    )

    assert response.status_code == 409


def test_start_session_without_active_deployment_is_409(
    client, workflow_auth
) -> None:
    workflow_id = _create_workflow(client, workflow_auth["alice"])
    _publish(client, workflow_auth["alice"], workflow_id, "v1")

    response = client.post(
        "/sessions",
        json={"workflow_id": workflow_id},
        headers=workflow_auth["alice"],
    )

    assert response.status_code == 409


def test_start_session_with_disabled_workflow_is_409(
    client, workflow_auth
) -> None:
    workflow_id = _create_workflow(client, workflow_auth["alice"])
    _fully_promote(client, workflow_auth, workflow_id)
    disabled = client.post(
        f"/workflows/{workflow_id}/disable", headers=workflow_auth["alice"]
    )
    assert disabled.status_code == 200, disabled.text

    response = client.post(
        "/sessions",
        json={"workflow_id": workflow_id},
        headers=workflow_auth["alice"],
    )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_pinned_snapshot_drives_report_export_priorities_and_review(
    client, workflow_auth, monkeypatch
) -> None:
    workflow_id = _create_workflow(client, workflow_auth["alice"])
    _fully_promote(client, workflow_auth, workflow_id)
    session = _start_routed_session(client, workflow_auth, workflow_id)
    session_id = session["session_id"]
    await _replace_template_id(session_id, "removed_template")
    monkeypatch.setattr(
        routing,
        "get_template",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("built-in template used")
        ),
    )

    report = client.get(
        f"/sessions/{session_id}/report", headers=workflow_auth["alice"]
    )
    exported = client.get(
        f"/sessions/{session_id}/report/export", headers=workflow_auth["alice"]
    )
    priorities = client.get(
        f"/sessions/{session_id}/priorities", headers=workflow_auth["alice"]
    )
    review = client.post(
        f"/sessions/{session_id}/review",
        json={
            "node": "failure_handling",
            "reviewer": "reviewer",
            "decision": "confirm",
        },
        headers=workflow_auth["alice"],
    )

    assert report.status_code == 200, report.text
    assert report.json()["template_id"] == "backend_engineer"
    assert exported.status_code == 200, exported.text
    assert "Template: `backend_engineer`" in exported.text
    assert priorities.status_code == 200, priorities.text
    assert {item["node"] for item in priorities.json()["priorities"]} == {
        "failure_handling",
        "consistency",
        "incident_response",
    }
    assert review.status_code == 200, review.text


def test_channel_session_routes_to_workflow(client, workflow_auth) -> None:
    workflow_id = _create_workflow(client, workflow_auth["alice"])
    deployment = _fully_promote(client, workflow_auth, workflow_id)

    response = client.post(
        "/channels/web/sessions",
        json={"external_id": "candidate-1", "workflow_id": workflow_id},
        headers=workflow_auth["alice"],
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["template_id"] == "backend_engineer"
    assert body["workflow_id"] == workflow_id
    assert body["workflow_version"] == 1
    assert body["workflow_deployment_id"] == deployment["deployment_id"]


def test_channel_unknown_workflow_is_404(client, workflow_auth) -> None:
    response = client.post(
        "/channels/web/sessions",
        json={"external_id": "candidate-unknown", "workflow_id": str(uuid4())},
        headers=workflow_auth["alice"],
    )

    assert response.status_code == 404


def test_resuming_channel_session_keeps_original_pin(
    client, workflow_auth
) -> None:
    workflow_id = _create_workflow(client, workflow_auth["alice"])
    _fully_promote(client, workflow_auth, workflow_id)
    first = client.post(
        "/channels/web/sessions",
        json={"external_id": "candidate-2", "workflow_id": workflow_id},
        headers=workflow_auth["alice"],
    )
    assert first.status_code == 200, first.text

    second = client.post(
        "/channels/web/sessions",
        json={"external_id": "candidate-2", "workflow_id": str(uuid4())},
        headers=workflow_auth["alice"],
    )

    assert second.status_code == 200, second.text
    assert second.json()["workflow_id"] == workflow_id
    assert second.json()["template_id"] == "backend_engineer"
    assert second.json()["workflow_version"] == 1


@pytest.mark.asyncio
async def test_promotion_after_session_creation_does_not_change_pin(
    client, workflow_auth
) -> None:
    workflow_id = _create_workflow(client, workflow_auth["alice"])
    _fully_promote(client, workflow_auth, workflow_id)
    session = _start_routed_session(client, workflow_auth, workflow_id)

    updated = client.patch(
        f"/workflows/{workflow_id}",
        json={"template_id": "data_engineer"},
        headers=workflow_auth["alice"],
    )
    assert updated.status_code == 200, updated.text
    _fully_promote(client, workflow_auth, workflow_id, key="v2")

    row = await db.get_session_row(session["session_id"])
    report = client.get(
        f"/sessions/{session['session_id']}/report",
        headers=workflow_auth["alice"],
    )
    assert row["workflow_version"] == 1
    assert report.status_code == 200, report.text
    assert report.json()["template_id"] == "backend_engineer"
