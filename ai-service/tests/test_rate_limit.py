"""Rate limiting and per-session turn authorization."""

from __future__ import annotations

from fastapi.testclient import TestClient
from pyinterviewbot_ai import db, main
from pyinterviewbot_ai.rate_limit import SlidingWindowLimiter


def test_sliding_window_limiter() -> None:
    limiter = SlidingWindowLimiter(limit=2, window_seconds=10)
    assert limiter.allow("k", now=0)[0] is True
    assert limiter.allow("k", now=1)[0] is True
    allowed, retry = limiter.allow("k", now=2)
    assert allowed is False
    assert retry == 8
    assert limiter.allow("k", now=11)[0] is True
    assert limiter.allow("other", now=11)[0] is True


def test_rate_limit_returns_429(
    client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    monkeypatch.setattr(main, "limiter", SlidingWindowLimiter(limit=2, window_seconds=60))
    assert client.get("/templates", headers=auth_headers).status_code == 200
    assert client.get("/templates", headers=auth_headers).status_code == 200
    response = client.get("/templates", headers=auth_headers)
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) >= 1


def test_health_is_not_rate_limited(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(main, "limiter", SlidingWindowLimiter(limit=1, window_seconds=60))
    for _ in range(5):
        assert client.get("/health").status_code == 200


def test_session_token_is_required_when_enabled(
    client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    monkeypatch.setenv("PYINTERVIEWBOT_REQUIRE_SESSION_TOKEN", "1")
    started = client.post(
        "/sessions", json={"template_id": "backend_engineer"}, headers=auth_headers
    )
    assert started.status_code == 200, started.text
    body = started.json()
    session_id = body["session_id"]
    token = body["session_token"]
    assert token

    payload = {"transcript": "I owned the retry and idempotency design end to end."}
    assert (
        client.post(f"/sessions/{session_id}/turn", json=payload, headers=auth_headers).status_code
        == 403
    )
    wrong = {**auth_headers, "X-Session-Token": "wrong"}
    assert (
        client.post(f"/sessions/{session_id}/turn", json=payload, headers=wrong).status_code
        == 403
    )
    right = {**auth_headers, "X-Session-Token": token}
    response = client.post(f"/sessions/{session_id}/turn", json=payload, headers=right)
    assert response.status_code == 200, response.text


def test_session_token_is_hashed_at_rest(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    started = client.post("/sessions", json={}, headers=auth_headers).json()
    row = client.portal.call(db.get_session_row, __import__("uuid").UUID(started["session_id"]))
    assert row["token_hash"]
    assert row["token_hash"] != started["session_token"]
