from uuid import uuid4

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
