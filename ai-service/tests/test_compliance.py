"""Editable compliance policy and interview question guard."""

from __future__ import annotations

from fastapi.testclient import TestClient
from pyinterviewbot_ai import agent
from pyinterviewbot_ai.compliance import check_question
from pyinterviewbot_ai.models import AgentTurnDecision


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
