"""Editable compliance policy and interview question guard."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pyinterviewbot_ai import agent
from pyinterviewbot_ai.compliance import check_question
from pyinterviewbot_ai.models import AgentTurnDecision

PRINCIPALS = {
    "alice-key": {"subject": "alice", "roles": ["owner", "author"]},
    "bob-key": {"subject": "bob", "roles": ["owner", "author"]},
}


@pytest.fixture
def tenant_auth(monkeypatch):
    monkeypatch.setenv("PYINTERVIEWBOT_PRINCIPALS", json.dumps(PRINCIPALS))
    return {
        "alice": {"X-API-Key": "alice-key"},
        "bob": {"X-API-Key": "bob-key"},
    }


class UnsafePlanner:
    async def decide(self, *_args, **_kwargs) -> AgentTurnDecision:
        return AgentTurnDecision(
            evidence_updates=[],
            next_action="probe",
            question_text="How old are you and when did you graduate?",
            is_complete=False,
        )


def test_default_policy_blocks_sensitive_topics() -> None:
    assert check_question("How old are you?").allowed is False
    assert check_question("What is your marital status?").allowed is False
    assert check_question("Are you a native English speaker?").allowed is False
    assert check_question("How would you design a retry policy?").allowed is True


def test_policy_can_be_reviewed_and_edited(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    policy = client.get("/compliance/policy", headers=auth_headers)
    assert policy.status_code == 200
    assert any(item["id"] == "age" for item in policy.json()["categories"])

    updated = client.put(
        "/compliance/policy/age",
        json={"label": "Age proxies", "patterns": [r"\bgraduation year\b"]},
        headers=auth_headers,
    )
    assert updated.status_code == 200, updated.text
    age = next(item for item in updated.json()["categories"] if item["id"] == "age")
    assert age["label"] == "Age proxies"
    assert age["patterns"] == [r"\bgraduation year\b"]

    disabled = client.put(
        "/compliance/policy/age", json={"enabled": False}, headers=auth_headers
    )
    assert disabled.status_code == 200
    age = next(item for item in disabled.json()["categories"] if item["id"] == "age")
    assert age["enabled"] is False

    reset = client.post("/compliance/policy/reset", headers=auth_headers)
    assert reset.status_code == 200
    age = next(item for item in reset.json()["categories"] if item["id"] == "age")
    assert age["enabled"] is True


def _age_category(response) -> dict:
    return next(
        item for item in response.json()["categories"] if item["id"] == "age"
    )


def test_policy_is_scoped_per_tenant(client: TestClient, tenant_auth) -> None:
    disabled = client.put(
        "/compliance/policy/age",
        json={"enabled": False},
        headers=tenant_auth["alice"],
    )
    assert disabled.status_code == 200, disabled.text
    assert _age_category(disabled)["enabled"] is False

    bob = client.get("/compliance/policy", headers=tenant_auth["bob"])
    assert _age_category(bob)["enabled"] is True

    assert client.put(
        "/compliance/policy/age",
        json={"enabled": False},
        headers=tenant_auth["bob"],
    ).status_code == 200
    alice_reset = client.post(
        "/compliance/policy/reset", headers=tenant_auth["alice"]
    )
    assert _age_category(alice_reset)["enabled"] is True
    bob_after = client.get("/compliance/policy", headers=tenant_auth["bob"])
    assert _age_category(bob_after)["enabled"] is False

    bob_reset = client.post(
        "/compliance/policy/reset", headers=tenant_auth["bob"]
    )
    assert _age_category(bob_reset)["enabled"] is True


def test_agent_uses_session_owner_policy(
    client: TestClient, tenant_auth, monkeypatch
) -> None:
    client.put(
        "/compliance/policy/age",
        json={"enabled": False},
        headers=tenant_auth["bob"],
    )
    monkeypatch.setattr(agent, "get_language_model", lambda: UnsafePlanner())

    started = client.post(
        "/sessions",
        json={"template_id": "behavioral_interview"},
        headers=tenant_auth["alice"],
    )

    assert started.status_code == 200, started.text
    question = started.json()["question_text"]
    assert "how old" not in question.lower()
    assert "graduate" not in question.lower()


def test_invalid_policy_regex_is_rejected(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.put(
        "/compliance/policy/age",
        json={"patterns": ["("]},
        headers=auth_headers,
    )
    assert response.status_code == 400


def test_agent_replaces_non_compliant_question(
    client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    monkeypatch.setattr(agent, "get_language_model", lambda: UnsafePlanner())
    response = client.post(
        "/sessions", json={"template_id": "behavioral_interview"}, headers=auth_headers
    )
    assert response.status_code == 200, response.text
    question = response.json()["question_text"]
    assert "how old" not in question.lower()
    assert "graduate" not in question.lower()
    assert question


def test_behavioral_template_is_listed(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    templates = client.get("/templates", headers=auth_headers).json()
    behavioral = next(item for item in templates if item["id"] == "behavioral_interview")
    assert behavioral["competency"] == "behavioral_competencies"
