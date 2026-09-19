"""SQLite persistence — the system of record for interview domain data
(spec §34: state must not live only inside the LLM context). One file per
deployment; path configurable via PYINTERVIEWBOT_DB_PATH.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID

import aiosqlite

from .evidence import COMPETENCY
from .models import EvidenceRecord, EvidenceStatus, SessionListItem, TranscriptSegment
from .resume import ResumeClaim

DB_PATH = os.environ.get("PYINTERVIEWBOT_DB_PATH", "data/pyinterviewbot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    resume_claim TEXT NOT NULL,
    turn_count INTEGER NOT NULL DEFAULT 0,
    is_complete INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transcript_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    turn_number INTEGER NOT NULL,
    speaker TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    competency TEXT NOT NULL,
    node TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT NOT NULL,
    turn_number INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


async def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


@asynccontextmanager
async def connect():
    db = await aiosqlite.connect(DB_PATH)
    try:
        yield db
    finally:
        await db.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def create_session(session_id: UUID, claim: ResumeClaim) -> None:
    async with connect() as db:
        await db.execute(
            "INSERT INTO sessions (id, created_at, resume_claim) VALUES (?, ?, ?)",
            (str(session_id), _now(), claim.text),
        )
        await db.commit()


async def append_transcript(session_id: UUID, turn_number: int, speaker: str, text: str) -> None:
    async with connect() as db:
        await db.execute(
            "INSERT INTO transcript_segments (session_id, turn_number, speaker, text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(session_id), turn_number, speaker, text, _now()),
        )
        await db.commit()


async def record_evidence(session_id: UUID, node: str, status: EvidenceStatus, note: str, turn_number: int) -> None:
    async with connect() as db:
        await db.execute(
            "INSERT INTO evidence (session_id, competency, node, status, note, turn_number, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(session_id), COMPETENCY, node, status, note, turn_number, _now()),
        )
        await db.commit()


async def set_turn_count(session_id: UUID, turn_count: int) -> None:
    async with connect() as db:
        await db.execute(
            "UPDATE sessions SET turn_count = ? WHERE id = ?",
            (turn_count, str(session_id)),
        )
        await db.commit()


async def mark_complete(session_id: UUID) -> None:
    async with connect() as db:
        await db.execute("UPDATE sessions SET is_complete = 1 WHERE id = ?", (str(session_id),))
        await db.commit()


async def list_sessions() -> list[SessionListItem]:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, created_at, resume_claim, is_complete, turn_count FROM sessions "
            "ORDER BY created_at DESC LIMIT 50"
        )
        rows = await cursor.fetchall()
        return [
            SessionListItem(
                session_id=r["id"],
                created_at=r["created_at"],
                resume_claim=r["resume_claim"],
                is_complete=bool(r["is_complete"]),
                turn_count=r["turn_count"],
            )
            for r in rows
        ]


async def get_session_row(session_id: UUID) -> aiosqlite.Row | None:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM sessions WHERE id = ?", (str(session_id),))
        return await cursor.fetchone()


async def get_transcript(session_id: UUID) -> list[TranscriptSegment]:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT turn_number, speaker, text FROM transcript_segments "
            "WHERE session_id = ? ORDER BY id ASC",
            (str(session_id),),
        )
        rows = await cursor.fetchall()
        return [TranscriptSegment(turn_number=r["turn_number"], speaker=r["speaker"], text=r["text"]) for r in rows]


async def get_evidence(session_id: UUID) -> list[EvidenceRecord]:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT competency, node, status, note, turn_number FROM evidence "
            "WHERE session_id = ? ORDER BY id ASC",
            (str(session_id),),
        )
        rows = await cursor.fetchall()
        return [
            EvidenceRecord(
                competency=r["competency"],
                node=r["node"],
                status=r["status"],
                note=r["note"],
                turn_number=r["turn_number"],
            )
            for r in rows
        ]
