"""Deterministic interview vertical slice: no models, no network, no paid APIs."""

from __future__ import annotations

from fastapi.testclient import TestClient
from pyinterviewbot_ai.main import app

ANSWERS = [
    "I led the Kafka ingestion redesign and owned retry idempotency across three services.",
    "We used outbox tables, idempotency keys, partition keys, and consumer offsets for ordering.",
    "During the outage I traced consumer lag, replayed a poison message, and added alerting.",
]


def _start(client: TestClient, headers: dict[str, str], template_id: str = "backend_engineer"):
    response = client.post(
        "/sessions", json={"template_id": template_id}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_mock_interview_flow_produces_evidence_linked_report(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    started = _start(client, auth_headers)
    session_id = started["session_id"]
    assert started["template_id"] == "backend_engineer"
    assert started["question_text"]

    for answer in ANSWERS:
        response = client.post(
            f"/sessions/{session_id}/turn",
            json={"transcript": answer},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text

    summary = client.get(f"/sessions/{session_id}/summary", headers=auth_headers)
    assert summary.status_code == 200
    body = summary.json()
    assert body["is_complete"] is True
    assert body["template_id"] == "backend_engineer"
    assert len(body["transcript"]) == 7
    assert len(body["evidence"]) == 3

    report = client.get(f"/sessions/{session_id}/report", headers=auth_headers)
    assert report.status_code == 200, report.text
    payload = report.json()
    assert payload["decision_support_only"] is True
    assert payload["human_review_required"] is True
    assert payload["evidence_coverage"] == 1.0
    assert payload["contradictions"] == 0
    assert len(payload["node_assessments"]) == 3
    for assessment in payload["node_assessments"]:
        assert assessment["citations"]
        citation = assessment["citations"][0]
        assert citation["quote"] in ANSWERS
        assert citation["note_is_model_inference"] is True
    assert any("not a hiring recommendation" in line for line in payload["limitations"])


def test_weak_answers_surface_uncertainty(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    session_id = _start(client, auth_headers, "data_engineer")["session_id"]
    for answer in ["It was fine.", "We used some tools.", "We fixed it."]:
        response = client.post(
            f"/sessions/{session_id}/turn",
            json={"transcript": answer},
            headers=auth_headers,
        )
        assert response.status_code == 200
    report = client.get(f"/sessions/{session_id}/report", headers=auth_headers).json()
    assert report["overall_uncertainty"] == "high"
    assert all(item["status"] == "weak" for item in report["node_assessments"])
    assert report["evidence_coverage"] == 1.0


def test_human_review_is_persisted_and_returned(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    session_id = _start(client, auth_headers)["session_id"]
    for answer in ANSWERS:
        client.post(
            f"/sessions/{session_id}/turn",
            json={"transcript": answer},
            headers=auth_headers,
        )
    review = client.post(
        f"/sessions/{session_id}/review",
        json={
            "node": "failure_handling",
            "reviewer": "reviewer-1",
            "decision": "needs_more_evidence",
            "note": "Please probe ownership again.",
        },
        headers=auth_headers,
    )
    assert review.status_code == 200, review.text
    report = client.get(f"/sessions/{session_id}/report", headers=auth_headers).json()
    first = report["node_assessments"][0]
    assert first["node"] == "failure_handling"
    assert first["human_review"][0]["decision"] == "needs_more_evidence"


def test_deletion_removes_report_and_summary(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    session_id = _start(client, auth_headers)["session_id"]
    deleted = client.delete(f"/sessions/{session_id}", headers=auth_headers)
    assert deleted.status_code == 200
    assert client.get(f"/sessions/{session_id}/summary", headers=auth_headers).status_code == 404
    assert client.get(f"/sessions/{session_id}/report", headers=auth_headers).status_code == 404


def test_unknown_template_and_node_are_rejected(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/sessions", json={"template_id": "nonexistent"}, headers=auth_headers
    )
    assert response.status_code == 400

    session_id = _start(client, auth_headers)["session_id"]
    review = client.post(
        f"/sessions/{session_id}/review",
        json={
            "node": "invented_node",
            "reviewer": "r",
            "decision": "confirm",
            "note": "",
        },
        headers=auth_headers,
    )
    assert review.status_code == 400


def test_audio_payload_limit_is_enforced(
    client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    monkeypatch.setattr("pyinterviewbot_ai.main.MAX_AUDIO_BYTES", 4)
    response = client.post(
        "/asr/transcribe",
        content=b"\x00\x00\x00\x00\x00",
        headers={**auth_headers, "content-type": "audio/l16;rate=16000"},
    )
    assert response.status_code == 413


def test_provider_discovery_and_templates(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    providers = client.get("/providers", headers=auth_headers)
    assert providers.status_code == 200
    body = providers.json()
    assert body["configured"] == {
        "asr": "mock",
        "tts": "mock",
        "llm": "mock",
    }
    assert all(item["healthy"] for item in body["status"])

    templates = client.get("/templates", headers=auth_headers)
    assert templates.status_code == 200
    ids = {item["id"] for item in templates.json()}
    assert {"rag_architect", "backend_engineer", "data_engineer"} <= ids


def test_non_health_endpoints_require_auth(client: TestClient) -> None:
    response = client.post(
        "/sessions",
        json={"template_id": "backend_engineer"},
        headers={"X-API-Key": "wrong"},
    )
    assert response.status_code == 401


def test_app_is_importable_without_network_access() -> None:
    assert app.title == "PyInterviewBot ai-service"
