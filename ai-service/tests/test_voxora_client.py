"""Contract tests for the PyInterviewBot -> Voxora client (ADR-3 subset).

The live Voxora service is not required for these tests: the fake transport
pins Voxora's documented request/response shapes, including contract-mismatch
error handling. The same request shapes appear in Voxora's contract tests, so
the two suites act as a consumer-side / producer-side pair.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

voxora_client = pytest.importorskip("pyinterviewbot_ai.voxora_client")
try:
    import httpx
except ImportError:  # pragma: no cover
    pytest.skip("httpx not installed", allow_module_level=True)

from pyinterviewbot_ai.voxora_client import (  # noqa: E402 - below guarded skip
    VoxoraClient,
    VoxoraClientError,
    VoxoraConfigError,
)

BASE = "https://voxora.test"
DECISION_BODY = {
    "decision": {"next_action": "probe", "question_text": "What was your role?", "is_complete": False},
    "source": "openrouter",
    "recorded_usage": {"prompt_tokens": 120.0, "completion_tokens": 40.0, "cost_usd": 0.0018},
}
SESSION_BODY = {
    "session_id": "vxs_123",
    "status": "completed",
    "simulated": True,
    "transcript_available": True,
    "events": [
        {"event_id": "vxe_1", "event_type": "voice_session.created", "event_version": "1.0", "occurred_at": "2026-11-03T00:00:00Z"},
        {"event_id": "vxe_2", "event_type": "voice_session.completed", "event_version": "1.0", "occurred_at": "2026-11-03T00:00:05Z"},
    ],
}
SESSION_PAYLOAD = {
    "external_application": "pyinterviewbot",
    "external_session_id": "interview_001",
    "contact": {"external_contact_id": "contact_001", "name": "Candidate One", "phone": "+15551234567"},
    "agent": {"agent_id": "recruitment_agent_01", "model_profile": "interview_default"},
    "session_metadata": {"candidate_id": "cand_001", "job_id": "job_001", "interview_template_id": "template_001"},
    "webhook_url": "https://pyinterviewbot.example.com/api/v1/webhooks/voxora",
}


def make_client(handler: Any, **kw: Any) -> VoxoraClient:
    transport = httpx.Client(transport=httpx.MockTransport(handler))
    return VoxoraClient(base_url=BASE, api_key="voxora-key", client=transport, **kw)


def test_decide_contract_auth_and_model_profile() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization")
        captured["app"] = request.headers.get("x-voxora-application")
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, json=DECISION_BODY)

    client = make_client(handler)
    decision = client.decide("Interview lead", evidence := "Q", model_profile="interview_default")
    assert decision["question_text"] == "What was your role?"
    assert captured["path"] == "/api/v1/ai/decide"
    assert captured["auth"] == "Bearer voxora-key"
    assert captured["app"] == "pyinterviewbot"
    assert captured["body"]["model_profile"] == "interview_default"
    _ = evidence


def test_voice_session_payload_matches_adr_shape() -> None:
    seen: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raw = request.read()
        seen.append(json.loads(raw) if raw else {})
        path = request.url.path
        if path.endswith("/voice-sessions"):
            return httpx.Response(201, json=SESSION_BODY)
        if path.endswith("/terminate"):
            return httpx.Response(200, json={**SESSION_BODY, "status": "terminated"})
        if path.endswith("/transcript"):
            return httpx.Response(200, json={"session_id": "vxs_123", "transcript_available": True, "entries": [{"role": "agent", "text": "Hello"}]})
        if path.endswith("/instructions"):
            return httpx.Response(
                409,
                json={
                    "error": {
                        "code": "instruction_unsupported",
                        "message": "Provider 'mock' does not support mid-call instruction injection",
                        "details": {"instruction": {"instruction_id": "i1"}, "provider_capabilities": {"mid_call_instructions": False}},
                    }
                },
            )
        raise AssertionError(f"unexpected path {path}")

    client = make_client(handler)
    session = client.create_voice_session(
        external_application="pyinterviewbot",
        external_session_id="interview_001",
        contact_external_id="contact_001",
        contact_name="Candidate One",
        phone="+15551234567",
        agent_id="recruitment_agent_01",
        model_profile="interview_default",
        session_metadata={"candidate_id": "cand_001", "job_id": "job_001", "interview_template_id": "template_001"},
        webhook_url="https://pyinterviewbot.example.com/api/v1/webhooks/voxora",
    )
    assert seen[0] == SESSION_PAYLOAD
    assert session["session_id"] == "vxs_123"
    assert session["events"][0]["event_type"] == "voice_session.created"
    assert client.terminate_voice_session("vxs_123")["status"] == "terminated"
    transcript = client.voice_session_transcript("vxs_123")
    assert transcript["transcript_available"] is True and transcript["entries"]
    with pytest.raises(VoxoraClientError, match="instruction_unsupported"):
        client.session_instruction("vxs_123", "i1", "Follow up on schema evolution")


def test_fail_closed_without_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VOXORA_BASE_URL", raising=False)
    monkeypatch.delenv("VOXORA_API_KEY", raising=False)
    with pytest.raises(VoxoraConfigError, match="VOXORA_BASE_URL"):
        VoxoraClient(api_key="k")
    with pytest.raises(VoxoraConfigError, match="VOXORA_API_KEY"):
        VoxoraClient(base_url=BASE)


def test_retries_retryable_statuses_only(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": {"code": "llm_unavailable"}})
        return httpx.Response(200, json=DECISION_BODY)

    client = make_client(handler, max_retries=1)
    assert client.decide("s", "u")["next_action"] == "probe"
    assert calls["n"] == 2
    calls["n"] = 0

    def handler_419(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, json={"error": {"code": "unknown_session"}})

    client = make_client(handler_419, max_retries=1)
    with pytest.raises(VoxoraClientError, match="voxora_http_404"):
        client.get_voice_session("vxs_none")
    assert calls["n"] == 1  # no retry budget burned on 4xx
