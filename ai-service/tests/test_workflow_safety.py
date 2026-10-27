"""Workflow safety properties: ownership, exact hashes, rollback, failures."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

PRINCIPALS = {
    "alice-key": {
        "subject": "alice",
        "roles": ["owner", "author", "deployer"],
    },
    "bob-key": {"subject": "bob", "roles": ["approver"]},
    "carol-key": {
        "subject": "carol",
        "roles": ["owner", "author", "approver", "deployer"],
    },
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
        "carol": {"X-API-Key": "carol-key"},
    }


def _create(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str = "Backend reliability",
    security: dict | None = None,
):
    response = client.post(
        "/workflows",
        json={
            "name": name,
            "description": "Pinned backend workflow.",
            "template_id": "backend_engineer",
            "runtime": {"capabilities": ["llm", "asr", "tts"]},
            "security": security
            or {
                "external_side_effects": False,
                "allow_external_network": False,
                "redact_transcripts": True,
                "require_human_review": True,
            },
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _publish(client: TestClient, headers: dict[str, str], workflow_id: str, key: str):
    response = client.post(
        f"/workflows/{workflow_id}/versions",
        headers={**headers, "Idempotency-Key": key},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _test_version(
    client: TestClient, headers: dict[str, str], workflow_id: str, version: int, key: str
):
    sandbox = client.post(
        f"/workflows/{workflow_id}/versions/{version}/sandbox/deploy",
        headers={**headers, "Idempotency-Key": f"sandbox-{key}"},
    )
    assert sandbox.status_code == 200, sandbox.text
    test_run = client.post(
        f"/workflows/{workflow_id}/versions/{version}/tests",
        json={"deployment_id": sandbox.json()["deployment_id"]},
        headers={**headers, "Idempotency-Key": f"tests-{key}"},
    )
    assert test_run.status_code == 200, test_run.text
    return sandbox.json(), test_run.json()


def _approve(
    client: TestClient,
    alice: dict[str, str],
    bob: dict[str, str],
    workflow_id: str,
    version: int,
    test_run_id: str,
    key: str,
):
    requested = client.post(
        f"/workflows/{workflow_id}/versions/{version}/approvals",
        json={
            "test_run_id": test_run_id,
            "approver_subject": "bob",
            "note": "Review sandbox evidence.",
        },
        headers={**alice, "Idempotency-Key": f"approval-{key}"},
    )
    assert requested.status_code == 200, requested.text
    approval_id = requested.json()["approval_id"]
    approved = client.post(
        f"/workflows/{workflow_id}/versions/{version}/approvals/{approval_id}/decision",
        json={"decision": "approve", "note": "Approved."},
        headers=bob,
    )
    assert approved.status_code == 200, approved.text
    return approval_id


def _promote(
    client: TestClient,
    headers: dict[str, str],
    workflow_id: str,
    version: int,
    test_run_id: str,
    approval_id: str,
    key: str,
):
    return client.post(
        f"/workflows/{workflow_id}/versions/{version}/production/deploy",
        json={"test_run_id": test_run_id, "approval_id": approval_id},
        headers={**headers, "Idempotency-Key": f"production-{key}"},
    )


def _fully_promote(
    client: TestClient,
    alice: dict[str, str],
    bob: dict[str, str],
    workflow_id: str,
    version: int,
    key: str,
):
    _, test_run = _test_version(client, alice, workflow_id, version, key)
    approval_id = _approve(
        client, alice, bob, workflow_id, version, test_run["test_run_id"], key
    )
    response = _promote(
        client,
        alice,
        workflow_id,
        version,
        test_run["test_run_id"],
        approval_id,
        key,
    )
    assert response.status_code == 200, response.text
    return test_run, approval_id, response.json()


def test_promotion_requires_an_approved_matching_hash(
    client: TestClient, workflow_auth: dict[str, dict[str, str]]
) -> None:
    alice = workflow_auth["alice"]
    bob = workflow_auth["bob"]
    workflow = _create(client, alice)
    workflow_id = workflow["workflow_id"]
    v1 = _publish(client, alice, workflow_id, "publish-1")
    _, test_run = _test_version(client, alice, workflow_id, 1, "v1")

    requested = client.post(
        f"/workflows/{workflow_id}/versions/1/approvals",
        json={
            "test_run_id": test_run["test_run_id"],
            "approver_subject": "bob",
            "note": "Awaiting decision.",
        },
        headers={**alice, "Idempotency-Key": "approval-pending"},
    )
    approval_id = requested.json()["approval_id"]

    blocked = _promote(
        client,
        alice,
        workflow_id,
        1,
        test_run["test_run_id"],
        approval_id,
        "blocked",
    )
    assert blocked.status_code == 409

    deployments = client.get(
        f"/workflows/{workflow_id}/deployments", headers=alice
    ).json()
    failed = [item for item in deployments if item["status"] == "failed"]
    assert len(failed) == 1
    assert failed[0]["content_hash"] == v1["content_hash"]
    assert "approval" in failed[0]["failure_reason"]

    approved = client.post(
        f"/workflows/{workflow_id}/versions/1/approvals/{approval_id}/decision",
        json={"decision": "approve", "note": "Now approved."},
        headers=bob,
    )
    assert approved.status_code == 200
    promoted = _promote(
        client,
        alice,
        workflow_id,
        1,
        test_run["test_run_id"],
        approval_id,
        "approved",
    )
    assert promoted.status_code == 200
    assert promoted.json()["status"] == "active"


def test_failed_promotion_replay_keeps_the_failure_status(
    client: TestClient, workflow_auth: dict[str, dict[str, str]]
) -> None:
    alice = workflow_auth["alice"]
    workflow = _create(client, alice)
    workflow_id = workflow["workflow_id"]
    _publish(client, alice, workflow_id, "publish-1")
    _, test_run = _test_version(client, alice, workflow_id, 1, "v1")
    requested = client.post(
        f"/workflows/{workflow_id}/versions/1/approvals",
        json={
            "test_run_id": test_run["test_run_id"],
            "approver_subject": "bob",
        },
        headers={**alice, "Idempotency-Key": "approval-pending"},
    )
    approval_id = requested.json()["approval_id"]

    first = _promote(
        client,
        alice,
        workflow_id,
        1,
        test_run["test_run_id"],
        approval_id,
        "failed-replay",
    )
    assert first.status_code == 409

    replay = _promote(
        client,
        alice,
        workflow_id,
        1,
        test_run["test_run_id"],
        approval_id,
        "failed-replay",
    )
    assert replay.status_code == 409
    assert replay.json()["detail"] == first.json()["detail"]


def test_rollback_rejects_an_idempotency_key_used_by_sandbox(
    client: TestClient, workflow_auth: dict[str, dict[str, str]]
) -> None:
    alice = workflow_auth["alice"]
    workflow = _create(client, alice)
    workflow_id = workflow["workflow_id"]
    _publish(client, alice, workflow_id, "publish-1")
    sandbox = client.post(
        f"/workflows/{workflow_id}/versions/1/sandbox/deploy",
        headers={**alice, "Idempotency-Key": "shared-key"},
    )
    assert sandbox.status_code == 200

    rollback = client.post(
        f"/workflows/{workflow_id}/rollback",
        json={"deployment_id": sandbox.json()["deployment_id"]},
        headers={**alice, "Idempotency-Key": "shared-key"},
    )
    assert rollback.status_code == 409


def test_failed_promotion_preserves_last_good_deployment(
    client: TestClient,
    workflow_auth: dict[str, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alice = workflow_auth["alice"]
    bob = workflow_auth["bob"]
    workflow = _create(client, alice)
    workflow_id = workflow["workflow_id"]

    v1 = _publish(client, alice, workflow_id, "publish-1")
    _, _, first = _fully_promote(client, alice, bob, workflow_id, 1, "v1")
    assert first["content_hash"] == v1["content_hash"]

    updated = client.patch(
        f"/workflows/{workflow_id}",
        json={"name": "Backend reliability v2"},
        headers=alice,
    )
    assert updated.status_code == 200
    v2 = _publish(client, alice, workflow_id, "publish-2")
    _, test_run = _test_version(client, alice, workflow_id, 2, "v2")
    approval_id = _approve(
        client, alice, bob, workflow_id, 2, test_run["test_run_id"], "v2"
    )

    monkeypatch.delenv("PYINTERVIEWBOT_PRODUCTION_BINDING_REVISION")
    failed = _promote(
        client,
        alice,
        workflow_id,
        2,
        test_run["test_run_id"],
        approval_id,
        "missing-binding",
    )
    assert failed.status_code == 409

    deployments = client.get(
        f"/workflows/{workflow_id}/deployments", headers=alice
    ).json()
    active = [item for item in deployments if item["environment"] == "production" and item["status"] == "active"]
    assert len(active) == 1
    assert active[0]["content_hash"] == v1["content_hash"]
    failed_rows = [
        item
        for item in deployments
        if item["environment"] == "production" and item["status"] == "failed"
    ]
    assert len(failed_rows) == 1
    assert failed_rows[0]["content_hash"] == v2["content_hash"]


def test_approval_decision_cannot_target_a_different_workflow_url(
    client: TestClient, workflow_auth: dict[str, dict[str, str]]
) -> None:
    alice = workflow_auth["alice"]
    bob = workflow_auth["bob"]
    first = _create(client, alice, name="First workflow")
    second = _create(client, alice, name="Second workflow")
    _publish(client, alice, first["workflow_id"], "publish-1")
    _, test_run = _test_version(client, alice, first["workflow_id"], 1, "v1")
    requested = client.post(
        f"/workflows/{first['workflow_id']}/versions/1/approvals",
        json={
            "test_run_id": test_run["test_run_id"],
            "approver_subject": "bob",
        },
        headers={**alice, "Idempotency-Key": "approval-1"},
    )
    approval_id = requested.json()["approval_id"]

    mismatched = client.post(
        f"/workflows/{second['workflow_id']}/versions/1/approvals/{approval_id}/decision",
        json={"decision": "approve", "note": "Wrong URL."},
        headers=bob,
    )
    assert mismatched.status_code == 404

    approvals = client.get(
        f"/workflows/{first['workflow_id']}/versions/1/approvals", headers=alice
    ).json()
    assert approvals[0]["status"] == "pending"


def test_promotion_rejects_test_evidence_for_a_different_artifact(
    client: TestClient, workflow_auth: dict[str, dict[str, str]]
) -> None:
    alice = workflow_auth["alice"]
    bob = workflow_auth["bob"]
    workflow = _create(client, alice)
    workflow_id = workflow["workflow_id"]

    v1 = _publish(client, alice, workflow_id, "publish-1")
    v1_test, v1_approval, _ = _fully_promote(
        client, alice, bob, workflow_id, 1, "v1"
    )

    client.patch(
        f"/workflows/{workflow_id}",
        json={"description": "Changed description creates a new hash."},
        headers=alice,
    )
    v2 = _publish(client, alice, workflow_id, "publish-2")
    assert v2["content_hash"] != v1["content_hash"]

    mismatched = _promote(
        client,
        alice,
        workflow_id,
        2,
        v1_test["test_run_id"],
        v1_approval,
        "mismatched",
    )
    assert mismatched.status_code == 409

    deployments = client.get(
        f"/workflows/{workflow_id}/deployments", headers=alice
    ).json()
    active = [
        item
        for item in deployments
        if item["environment"] == "production" and item["status"] == "active"
    ]
    assert len(active) == 1
    assert active[0]["content_hash"] == v1["content_hash"]


def test_rollback_returns_to_a_previously_promoted_hash(
    client: TestClient, workflow_auth: dict[str, dict[str, str]]
) -> None:
    alice = workflow_auth["alice"]
    bob = workflow_auth["bob"]
    workflow = _create(client, alice)
    workflow_id = workflow["workflow_id"]

    v1 = _publish(client, alice, workflow_id, "publish-1")
    _, _, first = _fully_promote(client, alice, bob, workflow_id, 1, "v1")

    client.patch(
        f"/workflows/{workflow_id}",
        json={"name": "Backend reliability v2"},
        headers=alice,
    )
    v2 = _publish(client, alice, workflow_id, "publish-2")
    _, _, second = _fully_promote(client, alice, bob, workflow_id, 2, "v2")
    assert second["content_hash"] == v2["content_hash"]

    rolled_back = client.post(
        f"/workflows/{workflow_id}/rollback",
        json={"deployment_id": first["deployment_id"]},
        headers={**alice, "Idempotency-Key": "rollback-1"},
    )
    assert rolled_back.status_code == 200, rolled_back.text
    assert rolled_back.json()["content_hash"] == v1["content_hash"]
    assert rolled_back.json()["version"] == 1

    deployments = client.get(
        f"/workflows/{workflow_id}/deployments", headers=alice
    ).json()
    active = [item for item in deployments if item["environment"] == "production" and item["status"] == "active"]
    assert len(active) == 1
    assert active[0]["content_hash"] == v1["content_hash"]
    assert any(item["status"] == "superseded" for item in deployments)


def test_rollback_requires_the_target_environment_binding(
    client: TestClient,
    workflow_auth: dict[str, dict[str, str]],
    monkeypatch,
) -> None:
    alice = workflow_auth["alice"]
    bob = workflow_auth["bob"]
    workflow = _create(client, alice)
    workflow_id = workflow["workflow_id"]

    _publish(client, alice, workflow_id, "publish-1")
    _, _, first = _fully_promote(client, alice, bob, workflow_id, 1, "v1")

    monkeypatch.setenv("PYINTERVIEWBOT_PRODUCTION_BINDING_REVISION", "prod-test-2")
    client.patch(
        f"/workflows/{workflow_id}",
        json={"name": "Backend reliability v2"},
        headers=alice,
    )
    _publish(client, alice, workflow_id, "publish-2")
    _fully_promote(client, alice, bob, workflow_id, 2, "v2")

    rejected = client.post(
        f"/workflows/{workflow_id}/rollback",
        json={"deployment_id": first["deployment_id"]},
        headers={**alice, "Idempotency-Key": "rollback-wrong-binding"},
    )
    assert rejected.status_code == 409


def test_rollback_fails_closed_when_the_binding_is_unavailable(
    client: TestClient,
    workflow_auth: dict[str, dict[str, str]],
    monkeypatch,
) -> None:
    alice = workflow_auth["alice"]
    bob = workflow_auth["bob"]
    workflow = _create(client, alice)
    workflow_id = workflow["workflow_id"]

    _publish(client, alice, workflow_id, "publish-1")
    _, _, first = _fully_promote(client, alice, bob, workflow_id, 1, "v1")
    monkeypatch.delenv("PYINTERVIEWBOT_PRODUCTION_BINDING_REVISION")

    rejected = client.post(
        f"/workflows/{workflow_id}/rollback",
        json={"deployment_id": first["deployment_id"]},
        headers={**alice, "Idempotency-Key": "rollback-no-binding"},
    )
    assert rejected.status_code == 409


def test_workflows_are_owned_and_approver_cannot_decide_for_someone_else(
    client: TestClient, workflow_auth: dict[str, dict[str, str]]
) -> None:
    alice = workflow_auth["alice"]
    bob = workflow_auth["bob"]
    carol = workflow_auth["carol"]
    workflow = _create(client, alice, name="Alice workflow")
    workflow_id = workflow["workflow_id"]

    assert client.get(f"/workflows/{workflow_id}", headers=bob).status_code == 403
    assert client.get("/workflows", headers=bob).json() == []
    assert client.post(
        "/workflows",
        json={"name": "Bob workflow", "template_id": "backend_engineer"},
        headers=bob,
    ).status_code == 403

    carol_create = client.post(
        "/workflows",
        json={"name": "Alice workflow", "template_id": "backend_engineer"},
        headers=carol,
    )
    assert carol_create.status_code == 200

    _publish(client, alice, workflow_id, "publish-1")
    _, test_run = _test_version(client, alice, workflow_id, 1, "v1")

    self_approval = client.post(
        f"/workflows/{workflow_id}/versions/1/approvals",
        json={
            "test_run_id": test_run["test_run_id"],
            "approver_subject": "alice",
        },
        headers={**alice, "Idempotency-Key": "self-approval"},
    )
    assert self_approval.status_code == 409

    requested = client.post(
        f"/workflows/{workflow_id}/versions/1/approvals",
        json={
            "test_run_id": test_run["test_run_id"],
            "approver_subject": "bob",
        },
        headers={**alice, "Idempotency-Key": "approval-bob"},
    )
    approval_id = requested.json()["approval_id"]
    wrong_approver = client.post(
        f"/workflows/{workflow_id}/versions/1/approvals/{approval_id}/decision",
        json={"decision": "approve", "note": "Not mine."},
        headers=carol,
    )
    assert wrong_approver.status_code == 403
    assert client.post(
        f"/workflows/{workflow_id}/versions/1/approvals/{approval_id}/decision",
        json={"decision": "approve", "note": "Mine."},
        headers=bob,
    ).status_code == 200


def test_security_failure_blocks_approval_and_records_failed_promotion(
    client: TestClient, workflow_auth: dict[str, dict[str, str]]
) -> None:
    alice = workflow_auth["alice"]
    workflow = _create(
        client,
        alice,
        security={
            "external_side_effects": True,
            "allow_external_network": True,
            "redact_transcripts": False,
            "require_human_review": False,
        },
    )
    workflow_id = workflow["workflow_id"]
    _publish(client, alice, workflow_id, "publish-1")
    _, test_run = _test_version(client, alice, workflow_id, 1, "unsafe")

    assert test_run["status"] == "failed"
    failed_suites = {
        item["name"] for item in test_run["evidence"] if not item["passed"]
    }
    assert "security_policy" in failed_suites

    approval = client.post(
        f"/workflows/{workflow_id}/versions/1/approvals",
        json={
            "test_run_id": test_run["test_run_id"],
            "approver_subject": "bob",
        },
        headers={**alice, "Idempotency-Key": "unsafe-approval"},
    )
    assert approval.status_code == 409

    production = _promote(
        client,
        alice,
        workflow_id,
        1,
        test_run["test_run_id"],
        "missing-approval",
        "unsafe-production",
    )
    assert production.status_code == 409
    deployments = client.get(
        f"/workflows/{workflow_id}/deployments", headers=alice
    ).json()
    assert any(
        item["status"] == "failed" and item["environment"] == "production"
        for item in deployments
    )


def test_disabled_workflow_cannot_be_deployed_until_reenabled(
    client: TestClient, workflow_auth: dict[str, dict[str, str]]
) -> None:
    alice = workflow_auth["alice"]
    workflow = _create(client, alice)
    workflow_id = workflow["workflow_id"]
    _publish(client, alice, workflow_id, "publish-1")

    assert client.post(f"/workflows/{workflow_id}/disable", headers=alice).status_code == 200
    blocked = client.post(
        f"/workflows/{workflow_id}/versions/1/sandbox/deploy",
        headers={**alice, "Idempotency-Key": "disabled-sandbox"},
    )
    assert blocked.status_code == 409

    assert client.post(f"/workflows/{workflow_id}/enable", headers=alice).status_code == 200
    allowed = client.post(
        f"/workflows/{workflow_id}/versions/1/sandbox/deploy",
        headers={**alice, "Idempotency-Key": "enabled-sandbox"},
    )
    assert allowed.status_code == 200
