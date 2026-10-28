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

SESSION_COLUMN_MIGRATIONS = (
    (
        "resume_enabled",
        "ALTER TABLE sessions ADD COLUMN resume_enabled INTEGER NOT NULL DEFAULT 1",
    ),
    (
        "resume_window_minutes",
        "ALTER TABLE sessions ADD COLUMN resume_window_minutes INTEGER NOT NULL DEFAULT 1440",
    ),
    (
        "resume_deadline",
        "ALTER TABLE sessions ADD COLUMN resume_deadline TEXT",
    ),
    (
        "resume_count",
        "ALTER TABLE sessions ADD COLUMN resume_count INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "workflow_id",
        "ALTER TABLE sessions ADD COLUMN workflow_id TEXT",
    ),
    (
        "workflow_version",
        "ALTER TABLE sessions ADD COLUMN workflow_version INTEGER",
    ),
    (
        "workflow_content_hash",
        "ALTER TABLE sessions ADD COLUMN workflow_content_hash TEXT",
    ),
    (
        "workflow_deployment_id",
        "ALTER TABLE sessions ADD COLUMN workflow_deployment_id TEXT",
    ),
    (
        "template_snapshot_json",
        "ALTER TABLE sessions ADD COLUMN template_snapshot_json TEXT",
    ),
    (
        "owner_subject",
        "ALTER TABLE sessions ADD COLUMN owner_subject TEXT",
    ),
)

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
    is_complete INTEGER NOT NULL DEFAULT 0,
    workflow_id TEXT,
    workflow_version INTEGER,
    workflow_content_hash TEXT,
    workflow_deployment_id TEXT,
    template_snapshot_json TEXT,
    owner_subject TEXT NOT NULL
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
    owner_subject TEXT NOT NULL,
    channel TEXT NOT NULL,
    external_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (owner_subject, channel, external_id)
);

CREATE TABLE IF NOT EXISTS channel_events (
    owner_subject TEXT NOT NULL,
    channel TEXT NOT NULL,
    event_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (owner_subject, channel, event_id)
);

CREATE TABLE IF NOT EXISTS compliance_policy (
    tenant_subject TEXT NOT NULL,
    category TEXT NOT NULL,
    label TEXT NOT NULL,
    description TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    patterns_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_subject, category)
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

CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    owner_subject TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    template_id TEXT NOT NULL,
    draft_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (owner_subject, name)
);

CREATE TABLE IF NOT EXISTS workflow_versions (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    artifact_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    idempotency_key TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (workflow_id, version),
    UNIQUE (workflow_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS workflow_test_runs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    environment TEXT NOT NULL,
    deployment_id TEXT NOT NULL,
    binding_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    idempotency_key TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (workflow_id, version)
        REFERENCES workflow_versions(workflow_id, version) ON DELETE CASCADE,
    UNIQUE (workflow_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS workflow_approvals (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    test_run_id TEXT NOT NULL REFERENCES workflow_test_runs(id),
    status TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    approver_subject TEXT NOT NULL,
    decided_by TEXT,
    request_note TEXT NOT NULL DEFAULT '',
    decision_note TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    FOREIGN KEY (workflow_id, version)
        REFERENCES workflow_versions(workflow_id, version) ON DELETE CASCADE,
    UNIQUE (workflow_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS workflow_deployments (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    environment TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    binding_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    previous_deployment_id TEXT,
    actor_subject TEXT NOT NULL,
    test_run_id TEXT,
    approval_id TEXT,
    idempotency_key TEXT,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    activated_at TEXT,
    FOREIGN KEY (workflow_id, version)
        REFERENCES workflow_versions(workflow_id, version) ON DELETE CASCADE,
    UNIQUE (workflow_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS workflow_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    version INTEGER,
    action TEXT NOT NULL,
    actor_subject TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    actor_subject TEXT NOT NULL,
    tenant_subject TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ai_audit_events_created
    ON ai_audit_events(created_at, id);

CREATE INDEX IF NOT EXISTS idx_workflow_versions_workflow
    ON workflow_versions(workflow_id, version);
CREATE INDEX IF NOT EXISTS idx_workflow_test_runs_version
    ON workflow_test_runs(workflow_id, version, created_at);
CREATE INDEX IF NOT EXISTS idx_workflow_approvals_version
    ON workflow_approvals(workflow_id, version, created_at);
CREATE INDEX IF NOT EXISTS idx_workflow_deployments_env
    ON workflow_deployments(workflow_id, environment, created_at);
CREATE INDEX IF NOT EXISTS idx_workflow_audit_workflow
    ON workflow_audit(workflow_id, id);

"""


async def _migrate_single_active_deployment(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        "UPDATE workflow_deployments SET status = 'superseded' "
        "WHERE status = 'active' AND rowid NOT IN ("
        "SELECT MAX(rowid) FROM workflow_deployments "
        "WHERE status = 'active' GROUP BY workflow_id, environment)"
    )
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_workflow_deployments_active_env "
        "ON workflow_deployments(workflow_id, environment) "
        "WHERE status = 'active'"
    )


def _legacy_owner_subject() -> str:
    configured = os.environ.get("PYINTERVIEWBOT_LEGACY_SESSION_OWNER") or os.environ.get(
        "PYINTERVIEWBOT_API_KEY_SUBJECT"
    )
    if configured:
        return configured
    if os.environ.get("PYINTERVIEWBOT_API_KEY"):
        return "local-operator"
    return "local-loopback"


async def _migrate_session_tenant(conn: aiosqlite.Connection) -> None:
    cursor = await conn.execute("PRAGMA table_info(sessions)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "owner_subject" not in columns:
        await conn.execute("ALTER TABLE sessions ADD COLUMN owner_subject TEXT")
    await conn.execute(
        "UPDATE sessions SET owner_subject = ("
        "SELECT owner_subject FROM workflows "
        "WHERE workflows.id = sessions.workflow_id) "
        "WHERE (owner_subject IS NULL OR owner_subject = '') "
        "AND workflow_id IS NOT NULL"
    )
    await conn.execute(
        "UPDATE sessions SET owner_subject = ? "
        "WHERE owner_subject IS NULL OR owner_subject = ''",
        (_legacy_owner_subject(),),
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_owner_created "
        "ON sessions(owner_subject, created_at DESC)"
    )

    cursor = await conn.execute("PRAGMA table_info(ai_audit_events)")
    audit_columns = {row[1] for row in await cursor.fetchall()}
    if "tenant_subject" not in audit_columns:
        await conn.execute(
            "ALTER TABLE ai_audit_events ADD COLUMN tenant_subject TEXT NOT NULL "
            "DEFAULT ''"
        )
    await conn.execute(
        "UPDATE ai_audit_events SET tenant_subject = actor_subject "
        "WHERE tenant_subject IS NULL OR tenant_subject = ''"
    )


async def _migrate_channel_tenant(conn: aiosqlite.Connection) -> None:
    cursor = await conn.execute("PRAGMA table_info(channel_sessions)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "owner_subject" in columns:
        return
    legacy_owner = _legacy_owner_subject()
    await conn.execute(
        "CREATE TABLE channel_sessions_new ("
        "owner_subject TEXT NOT NULL, channel TEXT NOT NULL, "
        "external_id TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(id), "
        "created_at TEXT NOT NULL, "
        "PRIMARY KEY (owner_subject, channel, external_id))"
    )
    await conn.execute(
        "INSERT INTO channel_sessions_new "
        "(owner_subject, channel, external_id, session_id, created_at) "
        "SELECT COALESCE((SELECT owner_subject FROM sessions "
        "WHERE sessions.id = channel_sessions.session_id), ?), "
        "channel, external_id, session_id, created_at FROM channel_sessions",
        (legacy_owner,),
    )
    await conn.execute("DROP TABLE channel_sessions")
    await conn.execute(
        "ALTER TABLE channel_sessions_new RENAME TO channel_sessions"
    )

    await conn.execute(
        "CREATE TABLE channel_events_new ("
        "owner_subject TEXT NOT NULL, channel TEXT NOT NULL, "
        "event_id TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(id), "
        "response_json TEXT NOT NULL, created_at TEXT NOT NULL, "
        "PRIMARY KEY (owner_subject, channel, event_id))"
    )
    await conn.execute(
        "INSERT INTO channel_events_new "
        "(owner_subject, channel, event_id, session_id, response_json, created_at) "
        "SELECT COALESCE((SELECT owner_subject FROM sessions "
        "WHERE sessions.id = channel_events.session_id), ?), "
        "channel, event_id, session_id, response_json, created_at FROM channel_events",
        (legacy_owner,),
    )
    await conn.execute("DROP TABLE channel_events")
    await conn.execute("ALTER TABLE channel_events_new RENAME TO channel_events")


async def _migrate_compliance_tenant(conn: aiosqlite.Connection) -> None:
    cursor = await conn.execute("PRAGMA table_info(compliance_policy)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "tenant_subject" in columns:
        return
    await conn.execute(
        "CREATE TABLE compliance_policy_new ("
        "tenant_subject TEXT NOT NULL, category TEXT NOT NULL, "
        "label TEXT NOT NULL, description TEXT NOT NULL, "
        "enabled INTEGER NOT NULL DEFAULT 1, patterns_json TEXT NOT NULL, "
        "updated_at TEXT NOT NULL, PRIMARY KEY (tenant_subject, category))"
    )
    await conn.execute(
        "INSERT OR IGNORE INTO compliance_policy_new "
        "(tenant_subject, category, label, description, enabled, "
        "patterns_json, updated_at) "
        "WITH owners(owner_subject) AS ("
        "SELECT ? UNION SELECT owner_subject FROM sessions "
        "WHERE owner_subject IS NOT NULL AND owner_subject != '') "
        "SELECT owners.owner_subject, policy.category, policy.label, "
        "policy.description, policy.enabled, policy.patterns_json, "
        "policy.updated_at "
        "FROM compliance_policy AS policy CROSS JOIN owners",
        (_legacy_owner_subject(),),
    )
    await conn.execute("DROP TABLE compliance_policy")
    await conn.execute(
        "ALTER TABLE compliance_policy_new RENAME TO compliance_policy"
    )


MIGRATIONS = (
    ("001_single_active_deployment", _migrate_single_active_deployment),
    ("002_session_tenant", _migrate_session_tenant),
    ("003_channel_tenant", _migrate_channel_tenant),
    ("004_compliance_tenant", _migrate_compliance_tenant),
)


async def _run_migrations(conn: aiosqlite.Connection) -> None:
    for name, migration in MIGRATIONS:
        cursor = await conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?", (name,)
        )
        if await cursor.fetchone() is not None:
            continue
        await migration(conn)
        await conn.execute(
            "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (name, _now()),
        )


async def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA busy_timeout = 5000")
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
        for name, statement in SESSION_COLUMN_MIGRATIONS:
            if name not in columns:
                await db.execute(statement)
        await _run_migrations(db)
        await db.commit()


@asynccontextmanager
async def connect():
    db = await aiosqlite.connect(DB_PATH)
    try:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA busy_timeout = 5000")
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
    *,
    workflow_id: str | None = None,
    workflow_version: int | None = None,
    workflow_content_hash: str | None = None,
    workflow_deployment_id: str | None = None,
    template_snapshot_json: str | None = None,
    owner_subject: str,
    audit_source: str,
) -> None:
    async with connect() as db:
        now = datetime.now(UTC)
        await db.execute(
            "INSERT INTO sessions "
            "(id, created_at, resume_claim, template_id, token_hash, "
            "resume_enabled, resume_window_minutes, resume_deadline, "
            "workflow_id, workflow_version, workflow_content_hash, "
            "workflow_deployment_id, template_snapshot_json, owner_subject) "
            "VALUES (?, ?, ?, ?, ?, 1, 1440, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(session_id),
                now.isoformat(),
                claim.text,
                template_id,
                token_hash,
                (now + timedelta(minutes=1440)).isoformat(),
                workflow_id,
                workflow_version,
                workflow_content_hash,
                workflow_deployment_id,
                template_snapshot_json,
                owner_subject,
            ),
        )
        details = {
            "source": audit_source,
            "template_id": template_id,
            "workflow_id": workflow_id,
            "workflow_version": workflow_version,
            "workflow_deployment_id": workflow_deployment_id,
            "workflow_content_hash": workflow_content_hash,
        }
        details = {key: value for key, value in details.items() if value is not None}
        await db.execute(
            "INSERT INTO ai_audit_events "
            "(action, actor_subject, tenant_subject, target_type, target_id, "
            "outcome, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "session.start",
                owner_subject,
                owner_subject,
                "session",
                str(session_id),
                "success",
                json.dumps(details, sort_keys=True),
                _now(),
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


async def record_ai_audit(
    *,
    action: str,
    actor_subject: str,
    target_type: str,
    target_id: str,
    outcome: str = "success",
    details: dict | None = None,
    tenant_subject: str | None = None,
) -> None:
    async with connect() as db:
        await db.execute(
            "INSERT INTO ai_audit_events "
            "(action, actor_subject, tenant_subject, target_type, target_id, "
            "outcome, details_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                action,
                actor_subject,
                tenant_subject or actor_subject,
                target_type,
                target_id,
                outcome,
                json.dumps(details or {}, sort_keys=True),
                _now(),
            ),
        )
        await db.commit()


async def delete_session(
    session_id: UUID,
    *,
    owner_subject: str | None = None,
    include_all: bool = False,
) -> bool:
    """Delete a session and all transcript/evidence rows (retention request)."""
    async with connect() as db:
        if not include_all:
            cursor = await db.execute(
                "SELECT 1 FROM sessions WHERE id = ? AND owner_subject = ?",
                (str(session_id), owner_subject),
            )
            if await cursor.fetchone() is None:
                return False
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


async def list_sessions(
    *,
    owner_subject: str | None = None,
    include_all: bool = False,
) -> list[SessionListItem]:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        query = (
            "SELECT id, created_at, resume_claim, template_id, is_complete, "
            "turn_count FROM sessions"
        )
        params: tuple[str, ...] = ()
        if not include_all:
            query += " WHERE owner_subject = ?"
            params = (owner_subject,)
        query += " ORDER BY created_at DESC LIMIT 50"
        cursor = await db.execute(query, params)
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


async def get_channel_session(
    owner_subject: str, channel: str, external_id: str
) -> str | None:
    async with connect() as db:
        cursor = await db.execute(
            "SELECT session_id FROM channel_sessions "
            "WHERE owner_subject = ? AND channel = ? AND external_id = ?",
            (owner_subject, channel, external_id),
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def bind_channel_session(
    owner_subject: str, channel: str, external_id: str, session_id: UUID
) -> bool:
    async with connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO channel_sessions "
            "(owner_subject, channel, external_id, session_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (owner_subject, channel, external_id, str(session_id), _now()),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_channel_event(
    owner_subject: str, channel: str, event_id: str
) -> dict | None:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT session_id, response_json FROM channel_events "
            "WHERE owner_subject = ? AND channel = ? AND event_id = ?",
            (owner_subject, channel, event_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "session_id": row["session_id"],
            "response": json.loads(row["response_json"]),
        }


async def record_channel_event(
    owner_subject: str,
    channel: str,
    event_id: str,
    session_id: UUID,
    response: dict,
) -> bool:
    async with connect() as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO channel_events "
            "(owner_subject, channel, event_id, session_id, response_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                owner_subject,
                channel,
                event_id,
                str(session_id),
                json.dumps(response),
                _now(),
            ),
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


async def _seed_default_compliance(
    db: aiosqlite.Connection, tenant_subject: str
) -> None:
    from .compliance import DEFAULT_CATEGORIES

    for category in DEFAULT_CATEGORIES:
        await db.execute(
            "INSERT OR IGNORE INTO compliance_policy "
            "(tenant_subject, category, label, description, enabled, "
            "patterns_json, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
            (
                tenant_subject,
                category.id,
                category.label,
                category.description,
                json.dumps(list(category.patterns)),
                _now(),
            ),
        )


async def list_compliance_categories(tenant_subject: str) -> list[dict]:
    async with connect() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT 1 FROM compliance_policy WHERE tenant_subject = ? LIMIT 1",
            (tenant_subject,),
        )
        if await cursor.fetchone() is None:
            await _seed_default_compliance(db, tenant_subject)
            await db.commit()
        cursor = await db.execute(
            "SELECT category, label, description, enabled, patterns_json, updated_at "
            "FROM compliance_policy WHERE tenant_subject = ? ORDER BY category ASC",
            (tenant_subject,),
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
    tenant_subject: str,
    category: str,
    *,
    label: str | None = None,
    description: str | None = None,
    enabled: bool | None = None,
    patterns: list[str] | None = None,
) -> bool:
    if label is None and description is None and enabled is None and patterns is None:
        return False
    async with connect() as db:
        cursor = await db.execute(
            "UPDATE compliance_policy SET "
            "label = COALESCE(?, label), "
            "description = COALESCE(?, description), "
            "enabled = COALESCE(?, enabled), "
            "patterns_json = COALESCE(?, patterns_json), "
            "updated_at = ? WHERE tenant_subject = ? AND category = ?",
            (
                label,
                description,
                None if enabled is None else int(enabled),
                None if patterns is None else json.dumps(patterns),
                _now(),
                tenant_subject,
                category,
            ),
        )
        await db.commit()
        return cursor.rowcount > 0


async def reset_compliance_policy(tenant_subject: str) -> None:
    async with connect() as db:
        await db.execute(
            "DELETE FROM compliance_policy WHERE tenant_subject = ?",
            (tenant_subject,),
        )
        await _seed_default_compliance(db, tenant_subject)
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
        await db.execute(
            """INSERT INTO interview_metadata
            (session_id, position_title, candidate_name, candidate_email,
             interviewer_name, interviewer_email, scheduled_at, duration_minutes,
             status, location, meeting_url, notes, created_at, updated_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                str(session_id),
                metadata.get("position_title"),
                metadata.get("candidate_name"),
                metadata.get("candidate_email"),
                metadata.get("interviewer_name"),
                metadata.get("interviewer_email"),
                metadata.get("scheduled_at"),
                metadata.get("duration_minutes", 60),
                metadata.get("status", "scheduled"),
                metadata.get("location"),
                metadata.get("meeting_url"),
                metadata.get("notes"),
                _now(),
                _now(),
            ),
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
    columns = (
        "position_title",
        "candidate_name",
        "candidate_email",
        "interviewer_name",
        "interviewer_email",
        "scheduled_at",
        "duration_minutes",
        "status",
        "location",
        "meeting_url",
        "notes",
    )
    if not any(metadata.get(column) is not None for column in columns):
        return False
    async with connect() as db:
        await db.execute(
            "UPDATE interview_metadata SET "
            "position_title = COALESCE(?, position_title), "
            "candidate_name = COALESCE(?, candidate_name), "
            "candidate_email = COALESCE(?, candidate_email), "
            "interviewer_name = COALESCE(?, interviewer_name), "
            "interviewer_email = COALESCE(?, interviewer_email), "
            "scheduled_at = COALESCE(?, scheduled_at), "
            "duration_minutes = COALESCE(?, duration_minutes), "
            "status = COALESCE(?, status), "
            "location = COALESCE(?, location), "
            "meeting_url = COALESCE(?, meeting_url), "
            "notes = COALESCE(?, notes), "
            "updated_at = ? WHERE session_id = ?",
            (
                *(metadata.get(column) for column in columns),
                _now(),
                str(session_id),
            ),
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
