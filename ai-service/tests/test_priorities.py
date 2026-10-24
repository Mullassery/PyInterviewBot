"""Recruiter priorities and red-flag reporting."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _start(client: TestClient, headers) -> str:
    return client.post(
        "/sessions", json={"template_id": "backend_engineer"}, headers=headers
    ).json()["session_id"]


def test_priorities_are_persisted_and_weight_report(
    client: TestClient, auth_headers
) -> None:
    session_id = _start(client, auth_headers)
    response = client.put(
        f"/sessions/{session_id}/priorities",
        json={
            "priorities": [
                {"node": "failure_handling", "priority": "must_have", "weight": 5},
                {"node": "consistency", "priority": "important", "weight": 3},
                {"node": "incident_response", "priority": "nice_to_have", "weight": 1},
            ]
        },
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    fetched = client.get(f"/sessions/{session_id}/priorities", headers=auth_headers)
    assert fetched.status_code == 200
    assert {item["node"]: item["priority"] for item in fetched.json()["priorities"]} == {
        "failure_handling": "must_have",
        "consistency": "important",
        "incident_response": "nice_to_have",
    }

    for answer in ["Fine.", "Some tools.", "We fixed it."]:
        turn = client.post(
            f"/sessions/{session_id}/turn",
            json={"transcript": answer},
            headers=auth_headers,
        )
        assert turn.status_code == 200

    report = client.get(f"/sessions/{session_id}/report", headers=auth_headers).json()
    assessment = next(
        item for item in report["node_assessments"] if item["node"] == "failure_handling"
    )
    assert assessment["priority"] == "must_have"
    assert assessment["priority_weight"] == 5
    assert assessment["priority_status"] == "partial"
    assert report["priority_weighted_coverage"] == 1.0
    assert any("failure_handling" in gap for gap in report["priority_gaps"])
    flags = {item["code"] for item in report["red_flags"]}
    assert "must_have_evidence_weak" in flags

    exported = client.get(
        f"/sessions/{session_id}/report/export?format=markdown", headers=auth_headers
    )
    assert "## Red flags" in exported.text
    assert "Priority-weighted coverage" in exported.text


def test_human_rejection_creates_red_flag(client: TestClient, auth_headers) -> None:
    session_id = _start(client, auth_headers)
    client.put(
        f"/sessions/{session_id}/priorities",
        json={"priorities": [{"node": "failure_handling", "priority": "must_have"}]},
        headers=auth_headers,
    )
    for answer in [
        "I owned retries and idempotency across three services.",
        "I reasoned about ordering with partition keys.",
        "I diagnosed consumer lag and added alerting.",
    ]:
        client.post(
            f"/sessions/{session_id}/turn", json={"transcript": answer}, headers=auth_headers
        )
    review = client.post(
        f"/sessions/{session_id}/review",
        json={
            "node": "failure_handling",
            "reviewer": "recruiter",
            "decision": "reject",
            "note": "Needs a stronger example.",
        },
        headers=auth_headers,
    )
    assert review.status_code == 200
    report = client.get(f"/sessions/{session_id}/report", headers=auth_headers).json()
    assert any(
        flag["code"] == "human_rejected_evidence" for flag in report["red_flags"]
    )


def test_unknown_priority_node_is_rejected(
    client: TestClient, auth_headers
) -> None:
    session_id = _start(client, auth_headers)
    response = client.put(
        f"/sessions/{session_id}/priorities",
        json={"priorities": [{"node": "invented", "priority": "must_have"}]},
        headers=auth_headers,
    )
    assert response.status_code == 400
