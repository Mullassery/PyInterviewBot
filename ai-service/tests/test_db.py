from uuid import uuid4

import aiosqlite
import pytest
from pyinterviewbot_ai import db
from pyinterviewbot_ai.resume import ResumeClaim


async def test_update_interview_metadata_rejects_injected_keys() -> None:
    session_id = uuid4()
    await db.init_db()
    await db.create_session(
        session_id,
        ResumeClaim(competency="x", text="x"),
        owner_subject="alice",
        audit_source="test",
    )
    await db.create_interview_metadata(
        session_id, {"position_title": "Engineer", "notes": "initial"}
    )

    updated = await db.update_interview_metadata(
        session_id,
        {
            "notes": "updated",
            "position_title = 'hacked'": "bad",
            "'; DROP TABLE sessions; --": "bad",
        },
    )

    assert updated is True
    row = await db.get_interview_metadata(session_id)
    assert row is not None
    assert row["position_title"] == "Engineer"
    assert row["notes"] == "updated"
    assert await db.get_session_row(session_id) is not None


async def test_init_db_migrates_session_workflow_columns(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "legacy.db"))
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
            "resume_claim TEXT NOT NULL)"
        )
        await conn.commit()
    await db.init_db()
    async with aiosqlite.connect(db.DB_PATH) as conn:
        cursor = await conn.execute("PRAGMA table_info(sessions)")
        columns = {row[1] for row in await cursor.fetchall()}
    assert {
        "workflow_id",
        "workflow_version",
        "workflow_content_hash",
        "workflow_deployment_id",
        "template_snapshot_json",
    } <= columns


async def test_create_session_persists_workflow_pin() -> None:
    session_id = uuid4()
    await db.init_db()
    await db.create_session(
        session_id,
        ResumeClaim(competency="x", text="x"),
        template_id="backend_engineer",
        workflow_id="00000000-0000-0000-0000-000000000003",
        workflow_version=2,
        workflow_content_hash="abc",
        workflow_deployment_id="deployment-1",
        template_snapshot_json='{"id": "backend_engineer"}',
        owner_subject="alice",
        audit_source="test",
    )
    row = await db.get_session_row(session_id)
    assert row["workflow_id"] == "00000000-0000-0000-0000-000000000003"
    assert row["workflow_version"] == 2
    assert row["workflow_content_hash"] == "abc"
    assert row["workflow_deployment_id"] == "deployment-1"
    assert row["template_snapshot_json"] == '{"id": "backend_engineer"}'


async def test_create_session_persists_owner_and_start_audit() -> None:
    session_id = uuid4()
    await db.init_db()
    await db.create_session(
        session_id,
        ResumeClaim(competency="x", text="secret claim text"),
        owner_subject="alice",
        audit_source="api",
        template_id="backend_engineer",
    )
    row = await db.get_session_row(session_id)
    assert row["owner_subject"] == "alice"
    async with db.connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT actor_subject, tenant_subject, action, outcome, details_json "
            "FROM ai_audit_events WHERE target_id = ?",
            (str(session_id),),
        )
        event = await cursor.fetchone()
    assert event["action"] == "session.start"
    assert event["actor_subject"] == "alice"
    assert event["tenant_subject"] == "alice"
    assert event["outcome"] == "success"
    assert "alice" not in event["details_json"]
    assert "secret claim text" not in event["details_json"]


async def test_init_db_backfills_session_owners_and_is_idempotent(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "legacy-owners.db"))
    monkeypatch.setenv("PYINTERVIEWBOT_LEGACY_SESSION_OWNER", "legacy-owner")
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
            "resume_claim TEXT NOT NULL, workflow_id TEXT)"
        )
        await conn.execute(
            "CREATE TABLE workflows (id TEXT PRIMARY KEY, owner_subject TEXT NOT NULL)"
        )
        await conn.execute(
            "INSERT INTO workflows (id, owner_subject) VALUES ('wf-1', 'alice')"
        )
        await conn.execute(
            "INSERT INTO sessions (id, created_at, resume_claim, workflow_id) "
            "VALUES ('s1', '2026-01-01', 'one', 'wf-1')"
        )
        await conn.execute(
            "INSERT INTO sessions (id, created_at, resume_claim, workflow_id) "
            "VALUES ('s2', '2026-01-02', 'two', NULL)"
        )
        await conn.commit()

    await db.init_db()
    await db.init_db()

    async with aiosqlite.connect(db.DB_PATH) as conn:
        cursor = await conn.execute(
            "SELECT id, owner_subject FROM sessions ORDER BY id"
        )
        owners = dict(await cursor.fetchall())
        cursor = await conn.execute("PRAGMA index_list(sessions)")
        indexes = {row[1] for row in await cursor.fetchall()}
    assert owners == {"s1": "alice", "s2": "legacy-owner"}
    assert "idx_sessions_owner_created" in indexes


async def test_init_db_backfills_channel_owner_from_session(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "legacy-channels.db"))
    await db.init_db()
    alice_session = uuid4()
    bob_session = uuid4()
    for session_id, owner in ((alice_session, "alice"), (bob_session, "bob")):
        await db.create_session(
            session_id,
            ResumeClaim(competency="x", text="x"),
            owner_subject=owner,
            audit_source="test",
        )
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute("DROP TABLE channel_sessions")
        await conn.execute("DROP TABLE channel_events")
        await conn.execute(
            "CREATE TABLE channel_sessions (channel TEXT NOT NULL, "
            "external_id TEXT NOT NULL, session_id TEXT NOT NULL, "
            "created_at TEXT NOT NULL, PRIMARY KEY (channel, external_id))"
        )
        await conn.execute(
            "CREATE TABLE channel_events (channel TEXT NOT NULL, "
            "event_id TEXT NOT NULL, session_id TEXT NOT NULL, "
            "response_json TEXT NOT NULL, created_at TEXT NOT NULL, "
            "PRIMARY KEY (channel, event_id))"
        )
        await conn.execute(
            "INSERT INTO channel_sessions VALUES ('web', 'shared', ?, 'now')",
            (str(alice_session),),
        )
        await conn.execute(
            "INSERT INTO channel_sessions VALUES ('twilio_voice', 'other', ?, 'now')",
            (str(bob_session),),
        )
        await conn.execute(
            "INSERT INTO channel_events VALUES ('web', 'event-1', ?, '{}', 'now')",
            (str(alice_session),),
        )
        await conn.execute(
            "INSERT INTO channel_events VALUES ('web', 'event-2', ?, '{}', 'now')",
            (str(bob_session),),
        )
        await conn.execute(
            "DELETE FROM schema_migrations WHERE name = '003_channel_tenant'"
        )
        await conn.commit()

    await db.init_db()
    await db.init_db()

    async with aiosqlite.connect(db.DB_PATH) as conn:
        cursor = await conn.execute(
            "SELECT owner_subject, channel, external_id FROM channel_sessions "
            "ORDER BY channel"
        )
        sessions = await cursor.fetchall()
        cursor = await conn.execute(
            "SELECT owner_subject, channel, event_id FROM channel_events ORDER BY event_id"
        )
        events = await cursor.fetchall()
    assert sessions == [("bob", "twilio_voice", "other"), ("alice", "web", "shared")]
    assert events == [("alice", "web", "event-1"), ("bob", "web", "event-2")]


async def test_create_session_rolls_back_when_audit_insert_fails() -> None:
    session_id = uuid4()
    await db.init_db()
    async with db.connect() as conn:
        await conn.execute("DROP TABLE ai_audit_events")
        await conn.commit()

    with pytest.raises(aiosqlite.OperationalError):
        await db.create_session(
            session_id,
            ResumeClaim(competency="x", text="x"),
            owner_subject="alice",
            audit_source="test",
        )

    assert await db.get_session_row(session_id) is None


async def test_session_listing_and_delete_are_owner_scoped() -> None:
    alice_session = uuid4()
    bob_session = uuid4()
    await db.init_db()
    for session_id, owner in ((alice_session, "alice"), (bob_session, "bob")):
        await db.create_session(
            session_id,
            ResumeClaim(competency="x", text="x"),
            owner_subject=owner,
            audit_source="test",
        )

    assert [item.session_id for item in await db.list_sessions(owner_subject="alice")] == [
        alice_session
    ]
    assert len(await db.list_sessions(include_all=True)) == 2
    assert await db.delete_session(bob_session, owner_subject="alice") is False
    assert await db.get_session_row(bob_session) is not None
    assert await db.delete_session(alice_session, owner_subject="alice") is True
