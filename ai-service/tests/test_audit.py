"""AI-service audit coverage for reviewer, deletion, and export actions."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from pyinterviewbot_ai import db


def _events() -> list[tuple]:
    async def fetch() -> list[tuple]:
        async with db.connect() as conn:
            cursor = await conn.execute(
                "SELECT action, actor_subject, tenant_subject, target_type, target_id, "
                "outcome, details_json FROM ai_audit_events ORDER BY id"
            )
            return await cursor.fetchall()

    return asyncio.run(fetch())


def test_review_export_and_delete_are_audited(
    client: TestClient,
    auth_headers: dict[str, str],
    monkeypatch,
) -> None:
    started = client.post(
        "/sessions", json={"template_id": "backend_engineer"}, headers=auth_headers
    )
    assert started.status_code == 200
    session_id = started.json()["session_id"]

    reviewed = client.post(
        f"/sessions/{session_id}/review",
        json={
            "node": "failure_handling",
            "reviewer": "spoofed-name",
            "decision": "needs_more_evidence",
            "note": "Ask for the outage timeline.",
        },
        headers=auth_headers,
    )
    assert reviewed.status_code == 200

    exported = client.get(
        f"/sessions/{session_id}/report/export?format=markdown",
        headers=auth_headers,
    )
    assert exported.status_code == 200

    deleted = client.delete(f"/sessions/{session_id}", headers=auth_headers)
    assert deleted.status_code == 200

    events = _events()
    assert [event[0] for event in events] == [
        "session.start",
        "session.review",
        "report.export",
        "session.delete",
    ]
    assert all(event[1] == "test-tenant" for event in events)
    assert all(event[2] == "test-tenant" for event in events)
    assert all(event[3] == "session" for event in events)
    assert all(event[4] == session_id for event in events)
    assert all(event[5] == "success" for event in events)
    assert '"node": "failure_handling"' in events[1][6]
    assert '"format": "markdown"' in events[2][6]
