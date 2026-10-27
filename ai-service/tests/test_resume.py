"""Recruiter resume policy and outage-resume API."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _start(client: TestClient, headers) -> dict:
    response = client.post(
        "/sessions", json={"template_id": "backend_engineer"}, headers=headers
    )
    assert response.status_code == 200
    return response.json()


def test_resume_policy_and_resume_with_token(
    client: TestClient, auth_headers
) -> None:
    started = _start(client, auth_headers)
    session_id = started["session_id"]
    token = started["session_token"]
    policy = client.get(f"/sessions/{session_id}/resume-policy", headers=auth_headers)
    assert policy.status_code == 200
    assert policy.json()["enabled"] is True

    disabled = client.put(
        f"/sessions/{session_id}/resume-policy",
        json={"enabled": False, "window_minutes": 60},
        headers=auth_headers,
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    denied = client.post(
        f"/sessions/{session_id}/resume",
        headers={**auth_headers, "X-Session-Token": token},
    )
    assert denied.status_code == 403

    enabled = client.put(
        f"/sessions/{session_id}/resume-policy",
        json={"enabled": True, "window_minutes": 60},
        headers=auth_headers,
    )
    assert enabled.status_code == 200
    assert enabled.json()["deadline"]

    no_token = client.post(f"/sessions/{session_id}/resume", headers=auth_headers)
    assert no_token.status_code == 403
    resumed = client.post(
        f"/sessions/{session_id}/resume",
        headers={**auth_headers, "X-Session-Token": token},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["question_text"] == started["question_text"]
    assert resumed.json()["resume_count"] == 1


def test_completed_interview_cannot_resume(
    client: TestClient, auth_headers
) -> None:
    started = _start(client, auth_headers)
    session_id = started["session_id"]
    token = started["session_token"]
    for answer in [
        "I owned retry and idempotency across three services.",
        "I reasoned about ordering with partition keys.",
        "I diagnosed consumer lag and added alerting.",
    ]:
        client.post(
            f"/sessions/{session_id}/turn",
            json={"transcript": answer},
            headers=auth_headers,
        )
    response = client.post(
        f"/sessions/{session_id}/resume",
        headers={**auth_headers, "X-Session-Token": token},
    )
    assert response.status_code == 409


def test_unknown_resume_session_returns_404(
    client: TestClient, auth_headers
) -> None:
    response = client.post(
        "/sessions/00000000-0000-0000-0000-000000000000/resume",
        headers=auth_headers,
    )
    assert response.status_code == 404
