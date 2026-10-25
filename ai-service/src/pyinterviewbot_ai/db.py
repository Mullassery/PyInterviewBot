"""SQLite persistence — the system of record for interview domain data
(spec §34: state must not live only inside the LLM context). One file per
deployment; path configurable via PYINTERVIEWBOT_DB_PATH.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import aiosqlite

from .evidence import COMPETENCY
from .models import (
    EvidenceRecord,
    EvidenceStatus,
    PrioritySetting,
    ReviewCorrection,
    SessionListItem,
    TranscriptSegment,
)
from .resume import ResumeClaim

DB_PATH = os.environ.get("PYINTERVIEWBOT_DB_PATH", "data/pyinterviewbot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    resume_claim TEXT NOT NULL,
    template_id TEXT NOT NULL DEFAULT 'rag_architect',
    token_hash TEXT NOT NULL DEFAULT '',
    resume_enabled INTEGER NOT NULL DEFAULT 1,
    resume_window_minutes INTEGER NOT NULL DEFAULT 1440,
    resume_deadline TEXT,
    resume_count INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS channel_sessions (
    channel TEXT NOT NULL,
    external_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (channel, external_id)
);

CREATE TABLE IF NOT EXISTS channel_events (
    channel TEXT NOT NULL,
    event_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (channel, event_id)
);

CREATE TABLE IF NOT EXISTS compliance_policy (
    category TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    description TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    patterns_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_priorities (
    session_id TEXT NOT NULL REFERENCES sessions(id),
    node TEXT NOT NULL,
    priority TEXT NOT NULL,
    weight INTEGER,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, node)
);

CREATE TABLE IF NOT EXISTS review_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    node TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    decision TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS interview_metadata (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
    position_title TEXT,
    candidate_name TEXT,
    candidate_email TEXT,
    interviewer_name TEXT,
    interviewer_email TEXT,
    scheduled_at TEXT,
    duration_minutes INTEGER DEFAULT 60,
    status TEXT DEFAULT 'scheduled',
    location TEXT,
    meeting_url TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);


"""


async def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        cursor = await db.execute("PRAGMA table_info(sessions)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "template_id" not in columns:
            await db.execute(
                "ALTER TABLE sessions ADD COLUMN template_id TEXT NOT NULL "
                "DEFAULT 'rag_architect'"
            )
        if "token_hash" not in columns:
            await db.execute(
                "ALTER TABLE sessions ADD COLUMN token_hash TEXT NOT NULL DEFAULT ''"
            )
        for name, definition in (
            ("resume_enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("resume_window_minutes", "INTEGER NOT NULL DEFAULT 1440"),
            ("resume_deadline", "TEXT"),
            ("resume_count", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                await db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")
        from .compliance import DEFAULT_CATEGORIES

        for category in DEFAULT_CATEGORIES:
            await db.execute(
                "INSERT OR IGNORE INTO compliance_policy "
                "(category, label, description, enabled, patterns_json, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?)",
                (
                    category.id,
                    category.label,
                    category.description,
                    json.dumps(list(category.patterns)),
                    _now(),
                ),
            )
        await db.commit()


@asynccontextmanager
async def connect():
    db = await aiosqlite.connect(DB_PATH)
    try:
        yield db
    finally:
        await db.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def create_session(
    session_id: UUID,
    claim: ResumeClaim,
    template_id: str = "rag_architect",
    token_hash: str = "",
) -> None:
    async with connect() as db:
        now = datetime.now(UTC)
        await db.execute(
            "INSERT INTO sessions "
            "(id, created_at, resume_claim, template_id, token_hash, "
            "resume_enabled, resume_window_minutes, resume_deadline) "
            "VALUES (?, ?, ?, ?, ?, 1, 1440, ?)",
            (
                str(session_id),
                now.isoformat(),
                claim.text,
                template_id,
                token_hash,
                (now + timedelta(minutes=1440)).isoformat(),
            ),
        )
        await db.commit()


async def append_transcript(
    session_id: UUID, turn_number: int, speaker: str, text: str
) -> None:
    async with connect() as db:
        await db.execute(
            "INSERT INTO transcript_segments (session_id, turn_number, speaker, text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(session_id), turn_number, speaker, text, _now()),
        )
        await db.commit()


async def record_evidence(
    session_id: UUID,
    node: str,
    status: EvidenceStatus,
    note: str,
    turn_number: int,
    competency: str = COMPETENCY,
) -> None:
    async with connect() as db:
        await db.execute(
            "INSERT INTO evidence (session_id, competency, node, status, note, turn_number, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(session_id), competency, node, status, note, turn_number, _now()),
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
        await db.execute(
            "UPDATE sessions SET is_complete = 1 WHERE id = ?", (str(session_id),)
        )
        await db.commit()


async def delete_session(session_id: UUID) -> bool:
    """Delete a session and all transcript/evidence rows (retention request)."""
    async with connect() as db:
        await db.execute(
            "DELETE FROM session_priorities WHERE session_id = ?", (str(session_id),)
        )
        await db.execute(
            "DELETE FROM channel_events WHERE session_id = ?", (str(session_id),)
        )
        await db.execute(
            "DELETE FROM channel_sessions WHERE session_id = ?", (str(session_id),)
        )
        await db.execute(
            "DELETE FROM review_corrections WHERE session_id = ?", (str(session_id),)
        )
        await db.execute(
            "DELETE FROM evidence WHERE session_id = ?", (str(session_id),)
        )
        await db.execute(
            "DELETE FROM transcript_segments WHERE session_id = ?", (str(session_id),)
        )
        cursor = await db.execute(
            "DELETE FROM sessions WHERE id = ?", (str(session_id),)
        )
        await db.commit()
        return cursor.rowcount > 0


async def list_sessions() -> list[SessionListItem]:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, created_at, resume_claim, template_id, is_complete, turn_count "
            "FROM sessions ORDER BY created_at DESC LIMIT 50"
        )
        rows = await cursor.fetchall()
        return [
            SessionListItem(
                session_id=r["id"],
                created_at=r["created_at"],
                resume_claim=r["resume_claim"],
                template_id=r["template_id"] or "rag_architect",
                is_complete=bool(r["is_complete"]),
                turn_count=r["turn_count"],
            )
            for r in rows
        ]


async def get_session_row(session_id: UUID) -> aiosqlite.Row | None:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sessions WHERE id = ?", (str(session_id),)
        )
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
        return [
            TranscriptSegment(
                turn_number=r["turn_number"], speaker=r["speaker"], text=r["text"]
            )
            for r in rows
        ]


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


async def record_review(
    session_id: UUID,
    node: str,
    reviewer: str,
    decision: str,
    note: str,
) -> None:
    async with connect() as db:
        await db.execute(
            "INSERT INTO review_corrections "
            "(session_id, node, reviewer, decision, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(session_id), node, reviewer, decision, note, _now()),
        )
        await db.commit()


async def get_reviews(session_id: UUID) -> list[ReviewCorrection]:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT node, reviewer, decision, note, created_at FROM review_corrections "
            "WHERE session_id = ? ORDER BY id ASC",
            (str(session_id),),
        )
        rows = await cursor.fetchall()
        return [
            ReviewCorrection(
                node=r["node"],
                reviewer=r["reviewer"],
                decision=r["decision"],
                note=r["note"],
                created_at=r["created_at"],
            )
            for r in rows
        ]


async def get_channel_session(channel: str, external_id: str) -> str | None:
    async with connect() as db:
        cursor = await db.execute(
            "SELECT session_id FROM channel_sessions WHERE channel = ? AND external_id = ?",
            (channel, external_id),
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def bind_channel_session(
    channel: str, external_id: str, session_id: UUID
) -> bool:
    async with connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO channel_sessions "
            "(channel, external_id, session_id, created_at) VALUES (?, ?, ?, ?)",
            (channel, external_id, str(session_id), _now()),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_channel_event(channel: str, event_id: str) -> dict | None:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT session_id, response_json FROM channel_events "
            "WHERE channel = ? AND event_id = ?",
            (channel, event_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "session_id": row["session_id"],
            "response": json.loads(row["response_json"]),
        }


async def record_channel_event(
    channel: str, event_id: str, session_id: UUID, response: dict
) -> bool:
    async with connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO channel_events "
            "(channel, event_id, session_id, response_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (channel, event_id, str(session_id), json.dumps(response), _now()),
        )
        await db.commit()
        return cursor.rowcount > 0


async def set_session_priorities(
    session_id: UUID, priorities: list[PrioritySetting]
) -> None:
    async with connect() as db:
        await db.execute(
            "DELETE FROM session_priorities WHERE session_id = ?", (str(session_id),)
        )
        for item in priorities:
            await db.execute(
                "INSERT INTO session_priorities "
                "(session_id, node, priority, weight, updated_at) VALUES (?, ?, ?, ?, ?)",
                (str(session_id), item.node, item.priority, item.weight, _now()),
            )
        await db.commit()


async def get_session_priorities(session_id: UUID) -> list[PrioritySetting]:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT node, priority, weight FROM session_priorities "
            "WHERE session_id = ? ORDER BY node ASC",
            (str(session_id),),
        )
        rows = await cursor.fetchall()
        return [
            PrioritySetting(node=r["node"], priority=r["priority"], weight=r["weight"])
            for r in rows
        ]


async def list_compliance_categories() -> list[dict]:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT category, label, description, enabled, patterns_json, updated_at "
            "FROM compliance_policy ORDER BY category ASC"
        )
        rows = await cursor.fetchall()
        return [
            {
                "category": row["category"],
                "label": row["label"],
                "description": row["description"],
                "enabled": bool(row["enabled"]),
                "patterns": json.loads(row["patterns_json"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]


async def update_compliance_category(
    category: str,
    *,
    label: str | None = None,
    description: str | None = None,
    enabled: bool | None = None,
    patterns: list[str] | None = None,
) -> bool:
    fields: list[str] = []
    values: list[object] = []
    if label is not None:
        fields.append("label = ?")
        values.append(label)
    if description is not None:
        fields.append("description = ?")
        values.append(description)
    if enabled is not None:
        fields.append("enabled = ?")
        values.append(1 if enabled else 0)
    if patterns is not None:
        fields.append("patterns_json = ?")
        values.append(json.dumps(patterns))
    if not fields:
        return False
    fields.append("updated_at = ?")
    values.append(_now())
    values.append(category)
    async with connect() as db:
        cursor = await db.execute(
            f"UPDATE compliance_policy SET {', '.join(fields)} WHERE category = ?",
            values,
        )
        await db.commit()
        return cursor.rowcount > 0


async def reset_compliance_policy() -> None:
    from .compliance import DEFAULT_CATEGORIES

    async with connect() as db:
        await db.execute("DELETE FROM compliance_policy")
        for category in DEFAULT_CATEGORIES:
            await db.execute(
                "INSERT INTO compliance_policy "
                "(category, label, description, enabled, patterns_json, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?)",
                (
                    category.id,
                    category.label,
                    category.description,
                    json.dumps(list(category.patterns)),
                    _now(),
                ),
            )
        await db.commit()


async def set_resume_policy(
    session_id: UUID, enabled: bool, window_minutes: int
) -> bool:
    async with connect() as db:
        cursor = await db.execute(
            "SELECT created_at FROM sessions WHERE id = ?", (str(session_id),)
        )
        row = await cursor.fetchone()
        if row is None:
            return False
        created = datetime.fromisoformat(row[0])
        deadline = created + timedelta(minutes=window_minutes)
        await db.execute(
            "UPDATE sessions SET resume_enabled = ?, resume_window_minutes = ?, "
            "resume_deadline = ? WHERE id = ?",
            (1 if enabled else 0, window_minutes, deadline.isoformat(), str(session_id)),
        )
        await db.commit()
        return True


async def increment_resume_count(session_id: UUID) -> None:
    async with connect() as db:
        await db.execute(
            "UPDATE sessions SET resume_count = resume_count + 1 WHERE id = ?",
            (str(session_id),),
        )
        await db.commit()

async def create_interview_metadata(
    session_id: UUID,
    metadata: dict
) -> None:
    """Create or update interview metadata for a session."""
    async with connect() as db:
        now = _now()
        await db.execute(
            """INSERT INTO interview_metadata 
            (session_id, position_title, candidate_name, candidate_email, 
             interviewer_name, interviewer_email, scheduled_at, duration_minutes,
             status, location, meeting_url, notes, created_at, updated_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                position_title=excluded.position_title,
                candidate_name=excluded.candidate_name,
                candidate_email=excluded.candidate_email,
                interviewer_name=excluded.interviewer_name,
                interviewer_email=excluded.interviewer_email,
                scheduled_at=excluded.scheduled_at,
                duration_minutes=excluded.duration_minutes,
                status=excluded.status,
                location=excluded.location,
                meeting_url=excluded.meeting_url,
                notes=excluded.notes,
                updated_at=excluded.updated_at""",
            (
                session_id,
                metadata.get('position_title'),
                metadata.get('candidate_name'),
                metadata.get('candidate_email'),
                metadata.get('interviewer_name'),
                metadata.get('interviewer_email'),
                metadata.get('scheduled_at'),
                metadata.get('duration_minutes', 60),
                metadata.get('status', 'scheduled'),
                metadata.get('location'),
                metadata.get('meeting_url'),
                metadata.get('notes'),
                _now(),
                _now()
            )
        )
        await db.commit()


async def get_interview_metadata(session_id: UUID) -> dict | None:
    """Get interview metadata for a session."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM interview_metadata WHERE session_id = ?",
            (str(session_id),)
        )
        row = await cursor.fetchone()
        if row:
            return dict(row)
        return None


async def update_interview_metadata(
    session_id: UUID,
    metadata: dict
) -> bool:
    """Update interview metadata for a session."""
    async with connect() as db:
        fields = []
        values = []
        allowed_fields = [
            'position_title', 'candidate_name', 'candidate_email',
            'interviewer_name', 'interviewer_email', 'scheduled_at',
            'duration_minutes', 'status', 'location', 'meeting_url', 'notes'
        ]
        for key, value in metadata.items():
            if key in allowed_fields and value is not None:
                fields.append(f"{key} = ?")
                values.append(value)
        if not fields:
            return False
        fields.append("updated_at = ?")
        values.append(_now())
        values.append(session_id)
        async with connect() as db:
            await db.execute(
                f"UPDATE interview_metadata SET {', '.join(fields)} WHERE session_id = ?",
                values
            )
            await db.commit()
        return True


async def delete_interview_metadata(session_id: UUID) -> bool:
    """Delete interview metadata for a session."""
    async with connect() as db:
        cursor = await db.execute(
            "DELETE FROM interview_metadata WHERE session_id = ?",
            (str(session_id),)
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_interview_metadata(session_id: UUID) -> dict | None:
    """Get interview metadata for a session."""
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM interview_metadata WHERE session_id = ?",
            (str(session_id),)
        )
        row = await cursor.fetchone()
        if row:
            return dict(row)
        return None
