"""Channel-neutral session mapping and idempotent message handling."""

from __future__ import annotations

from fastapi.testclient import TestClient

ANSWERS = [
    "I led the Kafka ingestion redesign and owned retry idempotency across services.",
    "We used outbox tables and partition keys to reason about ordering and consistency.",
    "During the outage I traced consumer lag, replayed a poison message, and added alerting.",
]


def _start(client: TestClient, headers, channel: str, external_id: str = "candidate-1"):
    response = client.post(
        f"/channels/{channel}/sessions",
        json={"external_id": external_id, "template_id": "backend_engineer"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_channel_session_mapping_is_stable(client: TestClient, auth_headers) -> None:
    first = _start(client, auth_headers, "twilio_voice")
    second = _start(client, auth_headers, "twilio_voice")
    assert first["session_id"] == second["session_id"]
    assert first["question_text"] == second["question_text"]
    assert first["template_id"] == "backend_engineer"

    other_channel = _start(client, auth_headers, "whatsapp_text")
    assert other_channel["session_id"] != first["session_id"]


def test_channel_message_is_idempotent(client: TestClient, auth_headers) -> None:
    session = _start(client, auth_headers, "whatsapp_text")
    payload = {
        "external_id": "candidate-1",
        "event_id": "event-1",
        "text": ANSWERS[0],
    }
    first = client.post(
        "/channels/whatsapp_text/messages", json=payload, headers=auth_headers
    )
    assert first.status_code == 200, first.text
    duplicate = client.post(
        "/channels/whatsapp_text/messages", json=payload, headers=auth_headers
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["ai_text"] == first.json()["ai_text"]
    assert duplicate.json()["duplicate"] is True

    summary = client.get(
        f"/sessions/{session['session_id']}/summary", headers=auth_headers
    ).json()
    candidate_turns = [
        segment for segment in summary["transcript"] if segment["speaker"] == "candidate"
    ]
    assert len(candidate_turns) == 1


def test_channel_validation_and_state_errors(client: TestClient, auth_headers) -> None:
    unknown = client.post(
        "/channels/unknown/sessions",
        json={"external_id": "x"},
        headers=auth_headers,
    )
    assert unknown.status_code == 400

    before_start = client.post(
        "/channels/twilio_voice/messages",
        json={"external_id": "not-started", "event_id": "e1", "text": "hello"},
        headers=auth_headers,
    )
    assert before_start.status_code == 409


def test_completed_channel_session_rejects_new_messages(
    client: TestClient, auth_headers
) -> None:
    session = _start(client, auth_headers, "twilio_voice")
    for index, answer in enumerate(ANSWERS):
        response = client.post(
            "/channels/twilio_voice/messages",
            json={
                "external_id": "candidate-1",
                "event_id": f"e-{index}",
                "text": answer,
            },
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
    assert response.json()["is_complete"] is True

    after_complete = client.post(
        "/channels/twilio_voice/messages",
        json={"external_id": "candidate-1", "event_id": "late", "text": "one more"},
        headers=auth_headers,
    )
    assert after_complete.status_code == 409
    assert session["session_id"]


def test_cross_channel_reports_share_the_same_structure(
    client: TestClient, auth_headers
) -> None:
    voice = _start(client, auth_headers, "twilio_voice", "voice-candidate")
    text = _start(client, auth_headers, "whatsapp_text", "text-candidate")
    for index, answer in enumerate(ANSWERS):
        for channel, external_id in (
            ("twilio_voice", "voice-candidate"),
            ("whatsapp_text", "text-candidate"),
        ):
            response = client.post(
                f"/channels/{channel}/messages",
                json={
                    "external_id": external_id,
                    "event_id": f"{channel}-{index}",
                    "text": answer,
                },
                headers=auth_headers,
            )
            assert response.status_code == 200, response.text

    voice_report = client.get(
        f"/sessions/{voice['session_id']}/report", headers=auth_headers
    ).json()
    text_report = client.get(
        f"/sessions/{text['session_id']}/report", headers=auth_headers
    ).json()
    assert [item["node"] for item in voice_report["node_assessments"]] == [
        item["node"] for item in text_report["node_assessments"]
    ]
    assert [item["status"] for item in voice_report["node_assessments"]] == [
        item["status"] for item in text_report["node_assessments"]
    ]
    assert voice_report["competency"] == text_report["competency"]


def test_vapi_channel_uses_the_same_session_contract(
    client: TestClient, auth_headers
) -> None:
    session = _start(client, auth_headers, "vapi", "vapi-candidate")
    assert session["channel"] == "vapi"
    assert session["external_id"] == "vapi-candidate"
