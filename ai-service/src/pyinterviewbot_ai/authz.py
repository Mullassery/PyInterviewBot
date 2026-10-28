"""Tenant authorization helpers for session-scoped resources."""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException

from . import db
from .auth import Principal


def session_visible_to(principal: Principal, row) -> bool:
    return principal.has_role("admin") or row["owner_subject"] == principal.subject


async def authorized_session(session_id: UUID, principal: Principal):
    row = await db.get_session_row(session_id)
    if row is None or not session_visible_to(principal, row):
        raise HTTPException(status_code=404, detail=f"unknown session {session_id}")
    return row


__all__ = ["authorized_session", "session_visible_to"]
