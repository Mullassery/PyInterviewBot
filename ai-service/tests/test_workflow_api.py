"""End-to-end workflow API: draft, sandbox test, approval, production."""

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


def _create(client: TestClient, headers: dict[str, str], name: str = "Backend reliability"):
    response = client.post(
        "/workflows",
        json={
            "name": name,
            "description": "Pinned backend workflow.",
            "template_id": "backend_engineer",
            "runtime": {"capabilities": ["llm", "asr", "tts"]},
            "security": {
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


def _publish(client: TestClient, headers: dict[str, str], workflow_id: str):
    response = client.post(
        f"/workflows/{workflow_id}/versions",
        headers={**headers, "Idempotency-Key": "publish-1"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _sandbox_deploy(
    client: TestClient, headers: dict[str, str], workflow_id: str, version: int
):
    response = client.post(
        f"/workflows/{workflow_id}/versions/{version}/sandbox/deploy",
        json={},
        headers={**headers, "Idempotency-Key": "sandbox-1"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _run_tests(
    client: TestClient,
    headers: dict[str, str],
    workflow_id: str,
    version: int,
    deployment_id: str,
):
    response = client.post(
        f"/workflows/{workflow_id}/versions/{version}/tests",
        json={"deployment_id": deployment_id},
        headers={**headers, "Idempotency-Key": "tests-1"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_workflow_vertical_slice_promotes_the_exact_tested_hash(
    client: TestClient, workflow_auth: dict[str, dict[str, str]]
) -> None:
    alice = workflow_auth["alice"]
    bob = workflow_auth["bob"]

    workflow = _create(client, alice)
    workflow_id = workflow["workflow_id"]
    assert workflow["owner_subject"] == "alice"
    assert workflow["draft"]["template_id"] == "backend_engineer"

    version = _publish(client, alice, workflow_id)
    assert version["version"] == 1
    tested_hash = version["content_hash"]
    assert version["artifact"]["content_hash"] == tested_hash

    sandbox = _sandbox_deploy(client, alice, workflow_id, 1)
    assert sandbox["environment"] == "sandbox"
    assert sandbox["content_hash"] == tested_hash

    test_run = _run_tests(client, alice, workflow_id, 1, sandbox["deployment_id"])
    assert test_run["status"] == "passed"
    assert {item["name"] for item in test_run["evidence"]} >= {
        "schema_hash",
        "template_pinning",
        "security_policy",
        "sandbox_isolation",
        "mock_interview",
    }
    assert all(item["passed"] for item in test_run["evidence"])

    requested = client.post(
        f"/workflows/{workflow_id}/versions/1/approvals",
        json={
            "test_run_id": test_run["test_run_id"],
            "approver_subject": "bob",
            "note": "Please review the sandbox evidence.",
        },
        headers={**alice, "Idempotency-Key": "approval-request-1"},
    )
    assert requested.status_code == 200, requested.text
    approval = requested.json()
    assert approval["status"] == "pending"

    approved = client.post(
        f"/workflows/{workflow_id}/versions/1/approvals/{approval['approval_id']}/decision",
        json={"decision": "approve", "note": "Hash and evidence reviewed."},
        headers=bob,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    assert approved.json()["decided_by"] == "bob"

    dry_run = client.post(
        f"/workflows/{workflow_id}/versions/1/production/dry-run",
        json={
            "test_run_id": test_run["test_run_id"],
            "approval_id": approval["approval_id"],
        },
        headers=alice,
    )
    assert dry_run.status_code == 200, dry_run.text
    assert dry_run.json()["eligible"] is True
    assert dry_run.json()["content_hash"] == tested_hash

    production = client.post(
        f"/workflows/{workflow_id}/versions/1/production/deploy",
        json={
            "test_run_id": test_run["test_run_id"],
            "approval_id": approval["approval_id"],
        },
        headers={**alice, "Idempotency-Key": "production-1"},
    )
    assert production.status_code == 200, production.text
    deployment = production.json()
    assert deployment["environment"] == "production"
    assert deployment["content_hash"] == tested_hash
    assert deployment["status"] == "active"

    duplicate = client.post(
        f"/workflows/{workflow_id}/versions/1/production/deploy",
        json={
            "test_run_id": test_run["test_run_id"],
            "approval_id": approval["approval_id"],
        },
        headers={**alice, "Idempotency-Key": "production-1"},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["deployment_id"] == deployment["deployment_id"]

    deployments = client.get(
        f"/workflows/{workflow_id}/deployments", headers=alice
    )
    assert deployments.status_code == 200
    assert len(deployments.json()) == 2
    assert deployments.json()[-1]["content_hash"] == tested_hash

    audit = client.get(f"/workflows/{workflow_id}/audit", headers=alice)
    actions = [item["action"] for item in audit.json()]
    assert actions == [
        "workflow.created",
        "workflow.version_published",
        "deployment.sandbox.active",
        "test.passed",
        "approval.requested",
        "approval.approved",
        "deployment.production.active",
    ]
