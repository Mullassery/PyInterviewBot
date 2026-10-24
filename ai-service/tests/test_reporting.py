"""Report rendering and export contracts."""

from __future__ import annotations

import csv
import io
import json

from fastapi.testclient import TestClient


def _session_with_evidence(client: TestClient, headers) -> str:
    started = client.post(
        "/sessions", json={"template_id": "backend_engineer"}, headers=headers
    ).json()
    session_id = started["session_id"]
    response = client.post(
        f"/sessions/{session_id}/turn",
        json={
            "transcript": (
                "I owned the retry and idempotency design. "
                "<script>alert('xss')</script> was not part of the system."
            )
        },
        headers=headers,
    )
    assert response.status_code == 200
    return session_id


def test_export_markdown(client: TestClient, auth_headers) -> None:
    session_id = _session_with_evidence(client, auth_headers)
    response = client.get(
        f"/sessions/{session_id}/report/export?format=markdown", headers=auth_headers
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "attachment" in response.headers["content-disposition"]
    assert "# Interview assessment report" in response.text
    assert "Decision support only" in response.text
    assert "failure_handling" in response.text


def test_html_export_escapes_candidate_text(client: TestClient, auth_headers) -> None:
    session_id = _session_with_evidence(client, auth_headers)
    response = client.get(
        f"/sessions/{session_id}/report/export?format=html", headers=auth_headers
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "&lt;script&gt;" in response.text
    assert "<script>alert" not in response.text


def test_csv_export_is_parseable(client: TestClient, auth_headers) -> None:
    session_id = _session_with_evidence(client, auth_headers)
    response = client.get(
        f"/sessions/{session_id}/report/export?format=csv", headers=auth_headers
    )
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert rows
    assert rows[0]["session_id"] == session_id
    assert "node" in rows[0]


def test_json_export_contains_report_and_session(client: TestClient, auth_headers) -> None:
    session_id = _session_with_evidence(client, auth_headers)
    response = client.get(
        f"/sessions/{session_id}/report/export?format=json", headers=auth_headers
    )
    assert response.status_code == 200
    payload = json.loads(response.text)
    assert payload["report"]["session_id"] == session_id
    assert payload["session"]["is_complete"] is False


def test_unsupported_export_format_is_rejected(client: TestClient, auth_headers) -> None:
    session_id = _session_with_evidence(client, auth_headers)
    response = client.get(
        f"/sessions/{session_id}/report/export?format=pdf", headers=auth_headers
    )
    assert response.status_code == 400
