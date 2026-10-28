from uuid import uuid4

import aiosqlite
from pyinterviewbot_ai import db
from pyinterviewbot_ai.resume import ResumeClaim


async def test_update_interview_metadata_rejects_injected_keys() -> None:
    session_id = uuid4()
    await db.init_db()
    await db.create_session(session_id, ResumeClaim(competency="x", text="x"))
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
    )
    row = await db.get_session_row(session_id)
    assert row["workflow_id"] == "00000000-0000-0000-0000-000000000003"
    assert row["workflow_version"] == 2
    assert row["workflow_content_hash"] == "abc"
    assert row["workflow_deployment_id"] == "deployment-1"
    assert row["template_snapshot_json"] == '{"id": "backend_engineer"}'
