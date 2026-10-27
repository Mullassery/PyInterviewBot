"""Workflow persistence: immutable versions, idempotency, ownership."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from pyinterviewbot_ai import db, workflow_store
from pyinterviewbot_ai.workflow_models import (
    RuntimeRequirements,
    SecurityPolicy,
    WorkflowDraft,
)


def _draft(name: str = "Backend reliability") -> WorkflowDraft:
    return WorkflowDraft(
        name=name,
        description="Pinned workflow.",
        template_id="backend_engineer",
        runtime=RuntimeRequirements(capabilities=["llm"]),
        security=SecurityPolicy(),
    )


async def test_store_round_trips_draft_and_immutable_versions() -> None:
    await db.init_db()
    record = await workflow_store.create_workflow(
        owner_subject="alice", draft=_draft(), actor="alice"
    )

    assert record.owner_subject == "alice"
    assert record.status == "active"
    assert record.latest_version == 0
    fetched = await workflow_store.get_workflow(record.workflow_id)
    assert fetched is not None
    assert fetched.draft == _draft()

    first = await workflow_store.publish_version(
        record.workflow_id, actor="alice", idempotency_key="publish-1"
    )
    repeated = await workflow_store.publish_version(
        record.workflow_id, actor="alice", idempotency_key="publish-1"
    )
    second = await workflow_store.publish_version(
        record.workflow_id, actor="alice", idempotency_key="publish-2"
    )

    assert first.artifact.version == 1
    assert first.artifact.verify_content_hash() is True
    assert repeated.version_id == first.version_id
    assert second.artifact.version == 2
    assert second.artifact.content_hash != first.artifact.content_hash

    versions = await workflow_store.list_versions(record.workflow_id)
    assert [item.artifact.version for item in versions] == [1, 2]
    assert (await workflow_store.get_workflow(record.workflow_id)).latest_version == 2


async def test_draft_update_only_affects_future_versions() -> None:
    await db.init_db()
    record = await workflow_store.create_workflow(
        owner_subject="alice", draft=_draft(), actor="alice"
    )
    first = await workflow_store.publish_version(record.workflow_id, actor="alice")

    updated = await workflow_store.update_draft(
        record.workflow_id,
        actor="alice",
        draft=_draft(name="Backend reliability v2"),
    )
    assert updated.draft.name == "Backend reliability v2"

    second = await workflow_store.publish_version(record.workflow_id, actor="alice")
    assert first.artifact.name == "Backend reliability"
    assert second.artifact.name == "Backend reliability v2"
    assert (await workflow_store.list_versions(record.workflow_id))[0].artifact.name == (
        "Backend reliability"
    )


async def test_workflow_names_are_unique_per_owner_but_not_globally() -> None:
    await db.init_db()
    await workflow_store.create_workflow(
        owner_subject="alice", draft=_draft(), actor="alice"
    )
    await workflow_store.create_workflow(
        owner_subject="bob", draft=_draft(), actor="bob"
    )

    import pytest

    with pytest.raises(workflow_store.WorkflowConflictError):
        await workflow_store.create_workflow(
            owner_subject="alice", draft=_draft(), actor="alice"
        )


async def test_concurrent_publishes_allocate_distinct_versions() -> None:
    await db.init_db()
    record = await workflow_store.create_workflow(
        owner_subject="alice", draft=_draft(), actor="alice"
    )

    published = await asyncio.gather(
        *(
            workflow_store.publish_version(
                record.workflow_id,
                actor="alice",
                idempotency_key=f"publish-{index}",
            )
            for index in range(3)
        )
    )

    assert sorted(item.artifact.version for item in published) == [1, 2, 3]
    assert len({item.version_id for item in published}) == 3


async def test_get_version_rejects_a_tampered_artifact() -> None:
    await db.init_db()
    record = await workflow_store.create_workflow(
        owner_subject="alice", draft=_draft(), actor="alice"
    )
    published = await workflow_store.publish_version(
        record.workflow_id, actor="alice", idempotency_key="publish-1"
    )
    async with db.connect() as conn:
        cursor = await conn.execute(
            "SELECT artifact_json FROM workflow_versions "
            "WHERE workflow_id = ? AND version = ?",
            (str(record.workflow_id), published.version),
        )
        artifact = json.loads((await cursor.fetchone())[0])
        artifact["name"] = "Tampered"
        await conn.execute(
            "UPDATE workflow_versions SET artifact_json = ? "
            "WHERE workflow_id = ? AND version = ?",
            (json.dumps(artifact), str(record.workflow_id), published.version),
        )
        await conn.commit()

    with pytest.raises(workflow_store.WorkflowStateError, match="hash"):
        await workflow_store.get_version(record.workflow_id, published.version)


async def test_unknown_workflow_and_version_are_reported() -> None:
    await db.init_db()
    missing = uuid4()
    assert await workflow_store.get_workflow(missing) is None
    assert await workflow_store.get_version(missing, 1) is None
