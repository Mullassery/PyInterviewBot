"""Recruiter-controlled interview resume policy and resume validation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from . import db
from .auth import session_token_matches, session_token_required
from .models import ResumePolicyResponse, ResumeResponse


class ResumeError(Exception):
    status_code = 409


class ResumeNotFoundError(ResumeError):
    """The requested interview session does not exist."""

    status_code = 404


class ResumeDisabledError(ResumeError):
    """Resume is disabled for this interview."""

    status_code = 403


class ResumeExpiredError(ResumeError):
    """The resume window for this interview has expired."""

    status_code = 410


class ResumeUnauthorizedError(ResumeError):
    """The resume request lacks a valid session token."""

    status_code = 403


ResumeNotFound = ResumeNotFoundError
ResumeDisabled = ResumeDisabledError
ResumeExpired = ResumeExpiredError
ResumeUnauthorized = ResumeUnauthorizedError


def _deadline(row) -> datetime:
    value = row["resume_deadline"]
    if value:
        return datetime.fromisoformat(value)
    created = datetime.fromisoformat(row["created_at"])
    minutes = int(row["resume_window_minutes"] or 1440)
    return created + timedelta(minutes=minutes)


def policy_for(row) -> ResumePolicyResponse:
    return ResumePolicyResponse(
        session_id=UUID(row["id"]),
        enabled=bool(row["resume_enabled"]),
        window_minutes=int(row["resume_window_minutes"] or 1440),
        deadline=_deadline(row).isoformat(),
        resume_count=int(row["resume_count"] or 0),
    )


async def resume_session(
    session_id: UUID, session_token: str | None
) -> ResumeResponse:
    row = await db.get_session_row(session_id)
    if row is None:
        raise ResumeNotFoundError("unknown session")
    if bool(row["is_complete"]):
        raise ResumeError("interview is already complete")
    if not bool(row["resume_enabled"]):
        raise ResumeDisabledError("resume is not allowed for this interview")
    deadline = _deadline(row)
    if datetime.now(UTC) > deadline:
        raise ResumeExpiredError("resume window has expired")

    stored_hash = (row["token_hash"] or "").strip()
    if stored_hash:
        if not session_token_matches(stored_hash, session_token):
            raise ResumeUnauthorizedError("invalid session token")
    elif session_token_required():
        raise ResumeUnauthorizedError("session token required")

    transcript = await db.get_transcript(session_id)
    last_question = next(
        (segment.text for segment in reversed(transcript) if segment.speaker == "ai"),
        "",
    )
    if not last_question:
        raise ResumeError("no interview question is available to resume")

    await db.increment_resume_count(session_id)
    refreshed = await db.get_session_row(session_id)
    count = int(refreshed["resume_count"] or 0) if refreshed else 0
    return ResumeResponse(
        session_id=session_id,
        question_text=last_question,
        turn_count=int(row["turn_count"] or 0),
        resume_deadline=deadline.isoformat(),
        resume_count=count,
        is_complete=False,
    )


__all__ = [
    "ResumeDisabled",
    "ResumeDisabledError",
    "ResumeError",
    "ResumeExpired",
    "ResumeExpiredError",
    "ResumeNotFound",
    "ResumeNotFoundError",
    "ResumeUnauthorized",
    "ResumeUnauthorizedError",
    "policy_for",
    "resume_session",
]
