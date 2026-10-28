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


class ChannelNotFoundError(ChannelError):
    """The channel mapping or its interview session no longer exists."""

    status_code = 404


class ChannelNotStartedError(ChannelError):
    """A message arrived before the channel session was started."""

    status_code = 409


class ChannelCompleteError(ChannelError):
    """The interview behind this channel mapping is already complete."""

    status_code = 409


ChannelNotFound = ChannelNotFoundError
ChannelNotStarted = ChannelNotStartedError
ChannelComplete = ChannelCompleteError


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
    workflow_id: UUID | None = None,
    *,
    owner_subject: str,
    allow_cross_tenant: bool = False,
) -> ChannelSessionResponse:
    validate_channel(channel)
    existing = await db.get_channel_session(owner_subject, channel, external_id)
    if existing:
        session_id = UUID(existing)
        row = await db.get_session_row(session_id)
        if row is None or (
            row["owner_subject"] != owner_subject
            and not allow_cross_tenant
        ):
            raise ChannelNotFoundError("channel session points to a deleted interview")
        return ChannelSessionResponse(
            channel=channel,
            external_id=external_id,
            session_id=session_id,
            template_id=row["template_id"] or "rag_architect",
            question_text=await _last_question(session_id),
            new_session=False,
            workflow_id=row["workflow_id"],
            workflow_version=row["workflow_version"],
            workflow_deployment_id=row["workflow_deployment_id"],
        )

    result = await agent.start_session(
        template_id,
        workflow_id,
        owner_subject=owner_subject,
        allow_cross_tenant=allow_cross_tenant,
        source=f"channel:{channel}",
    )
    session_id = result.session_id
    template = result.template_id
    if not await db.bind_channel_session(
        owner_subject, channel, external_id, session_id
    ):
        await db.delete_session(session_id, owner_subject=owner_subject)
        existing = await db.get_channel_session(owner_subject, channel, external_id)
        if not existing:
            raise ChannelNotStartedError("could not bind channel session")
        session_id = UUID(existing)
        row = await db.get_session_row(session_id)
        if row is None or (
            row["owner_subject"] != owner_subject
            and not allow_cross_tenant
        ):
            raise ChannelNotFoundError("channel session points to a deleted interview")
    return ChannelSessionResponse(
        channel=channel,
        external_id=external_id,
        session_id=session_id,
        template_id=template,
        question_text=await _last_question(session_id),
        new_session=True,
        workflow_id=result.workflow_id,
        workflow_version=result.workflow_version,
        workflow_deployment_id=result.workflow_deployment_id,
    )


async def submit_message(
    channel: str,
    external_id: str,
    event_id: str,
    text: str,
    template_id: str | None,
    *,
    owner_subject: str,
    allow_cross_tenant: bool = False,
) -> ChannelMessageResponse:
    validate_channel(channel)
    existing_event = await db.get_channel_event(owner_subject, channel, event_id)
    if existing_event:
        stored = existing_event["response"]
        stored["duplicate"] = True
        return ChannelMessageResponse.model_validate(stored)

    session_id_text = await db.get_channel_session(
        owner_subject, channel, external_id
    )
    if not session_id_text:
        raise ChannelNotStartedError("start the channel session before sending messages")
    session_id = UUID(session_id_text)

    row = await db.get_session_row(session_id)
    if row is None or (
        row["owner_subject"] != owner_subject and not allow_cross_tenant
    ):
        raise ChannelNotFoundError("unknown interview session")
    if bool(row["is_complete"]):
        raise ChannelCompleteError("interview is already complete")

    turn = await agent.take_turn(session_id, text)
    response = {
        "channel": channel,
        "external_id": external_id,
        "session_id": str(session_id),
        "ai_text": turn.ai_text,
        "is_complete": turn.is_complete,
        "duplicate": False,
    }
    if not await db.record_channel_event(
        owner_subject, channel, event_id, session_id, response
    ):
        stored = await db.get_channel_event(owner_subject, channel, event_id)
        if stored:
            result = stored["response"]
            result["duplicate"] = True
            return ChannelMessageResponse.model_validate(result)
    return ChannelMessageResponse.model_validate(response)


__all__ = [
    "CHANNELS",
    "ChannelComplete",
    "ChannelCompleteError",
    "ChannelError",
    "ChannelNotFound",
    "ChannelNotFoundError",
    "ChannelNotStarted",
    "ChannelNotStartedError",
    "start_or_resume",
    "submit_message",
    "validate_channel",
]
