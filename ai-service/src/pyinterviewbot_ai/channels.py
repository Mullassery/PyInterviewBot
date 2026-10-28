"""Channel-neutral interview access with durable mapping and idempotency.

Channel adapters (Twilio, WhatsApp, Teams, web) call these functions. The
interview engine and evidence/report ownership stay in the existing agent.
"""

from __future__ import annotations

from uuid import UUID

from . import agent, db
from .models import ChannelMessageResponse, ChannelSessionResponse

CHANNELS = {
    "web",
    "twilio_voice",
    "vapi",
    "whatsapp_text",
    "whatsapp_voice_note",
    "teams",
}


class ChannelError(Exception):
    status_code = 400


class ChannelNotFound(ChannelError):
    status_code = 404


class ChannelNotStarted(ChannelError):
    status_code = 409


class ChannelComplete(ChannelError):
    status_code = 409


def validate_channel(channel: str) -> str:
    if channel not in CHANNELS:
        raise ChannelError(f"unsupported channel '{channel}'")
    return channel


async def _last_question(session_id) -> str:
    transcript = await db.get_transcript(session_id)
    for segment in reversed(transcript):
        if segment.speaker == "ai":
            return segment.text
    return ""


async def start_or_resume(
    channel: str,
    external_id: str,
    template_id: str | None,
) -> ChannelSessionResponse:
    validate_channel(channel)
    existing = await db.get_channel_session(channel, external_id)
    if existing:
        session_id = UUID(existing)
        row = await db.get_session_row(session_id)
        if row is None:
            raise ChannelNotFound("channel session points to a deleted interview")
        return ChannelSessionResponse(
            channel=channel,
            external_id=external_id,
            session_id=session_id,
            template_id=row["template_id"] or "rag_architect",
            question_text=await _last_question(session_id),
            new_session=False,
        )

    result = await agent.start_session(template_id)
    session_id = result.session_id
    template = result.template_id
    if not await db.bind_channel_session(channel, external_id, session_id):
        await db.delete_session(session_id)
        existing = await db.get_channel_session(channel, external_id)
        if not existing:
            raise ChannelNotStarted("could not bind channel session")
        session_id = UUID(existing)
        row = await db.get_session_row(session_id)
        if row is None:
            raise ChannelNotFound("channel session points to a deleted interview")
    return ChannelSessionResponse(
        channel=channel,
        external_id=external_id,
        session_id=session_id,
        template_id=template,
        question_text=await _last_question(session_id),
        new_session=True,
    )


async def submit_message(
    channel: str,
    external_id: str,
    event_id: str,
    text: str,
    template_id: str | None,
) -> ChannelMessageResponse:
    validate_channel(channel)
    existing_event = await db.get_channel_event(channel, event_id)
    if existing_event:
        stored = existing_event["response"]
        stored["duplicate"] = True
        return ChannelMessageResponse.model_validate(stored)

    session_id_text = await db.get_channel_session(channel, external_id)
    if not session_id_text:
        raise ChannelNotStarted("start the channel session before sending messages")
    session_id = UUID(session_id_text)

    row = await db.get_session_row(session_id)
    if row is None:
        raise ChannelNotFound("unknown interview session")
    if bool(row["is_complete"]):
        raise ChannelComplete("interview is already complete")

    turn = await agent.take_turn(session_id, text)
    response = {
        "channel": channel,
        "external_id": external_id,
        "session_id": str(session_id),
        "ai_text": turn.ai_text,
        "is_complete": turn.is_complete,
        "duplicate": False,
    }
    if not await db.record_channel_event(channel, event_id, session_id, response):
        stored = await db.get_channel_event(channel, event_id)
        if stored:
            result = stored["response"]
            result["duplicate"] = True
            return ChannelMessageResponse.model_validate(result)
    return ChannelMessageResponse.model_validate(response)


__all__ = [
    "CHANNELS",
    "ChannelComplete",
    "ChannelError",
    "ChannelNotFound",
    "ChannelNotStarted",
    "start_or_resume",
    "submit_message",
    "validate_channel",
]
