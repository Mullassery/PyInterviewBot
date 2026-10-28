from datetime import UTC, datetime
from uuid import UUID, uuid4

import aiosqlite
import pytest
from pyinterviewbot_ai import agent, db, workflow_store
from pyinterviewbot_ai import workflow_routing as routing
from pyinterviewbot_ai.providers.base import EvidenceNodeSpec
from pyinterviewbot_ai.templates import InterviewTemplate, get_template
from pyinterviewbot_ai.workflow_env import PRODUCTION_REVISION_ENV, production_binding
from pyinterviewbot_ai.workflow_models import WorkflowDraft
from pyinterviewbot_ai.workflow_store import WorkflowVersionRecord


async def _published_workflow(
    monkeypatch,
) -> tuple[UUID, WorkflowVersionRecord]:
    monkeypatch.setenv(PRODUCTION_REVISION_ENV, "prod-test-1")
    await db.init_db()
    workflow = await workflow_store.create_workflow(
        owner_subject="alice",
        draft=WorkflowDraft(name="Routing", template_id="backend_engineer"),
        actor="alice",
    )
    version = await workflow_store.publish_version(
        workflow.workflow_id, actor="alice"
    )
    return workflow.workflow_id, version


async def _insert_deployment(
    workflow_id: UUID,
    version: WorkflowVersionRecord,
    *,
    deployment_id: str = "deployment-1",
    content_hash: str | None = None,
    binding_fingerprint: str | None = None,
    deployment_version: int | None = None,
) -> str:
    binding = production_binding()
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO workflow_deployments "
            "(id, workflow_id, version, environment, content_hash, "
            "binding_fingerprint, status, actor_subject, created_at, activated_at) "
            "VALUES (?, ?, ?, 'production', ?, ?, 'active', 'alice', ?, ?)",
            (
                deployment_id,
                str(workflow_id),
                deployment_version or version.version,
                content_hash or version.content_hash,
                binding_fingerprint or binding.fingerprint,
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
            ),
        )
        await conn.commit()
    return deployment_id


async def _active_route(monkeypatch) -> tuple[UUID, WorkflowVersionRecord, str]:
    workflow_id, version = await _published_workflow(monkeypatch)
    deployment_id = await _insert_deployment(workflow_id, version)
    return workflow_id, version, deployment_id


async def test_start_session_with_workflow_pins_snapshot(monkeypatch) -> None:
    workflow_id, version, deployment_id = await _active_route(monkeypatch)

    result = await agent.start_session(
        "ignored_template", workflow_id, owner_subject="alice"
    )

    row = await db.get_session_row(result.session_id)
    assert result.template_id == "backend_engineer"
    assert result.workflow_id == workflow_id
    assert result.workflow_version == 1
    assert result.workflow_deployment_id == deployment_id
    assert row["workflow_id"] == str(workflow_id)
    assert row["workflow_version"] == 1
    assert row["workflow_content_hash"] == version.content_hash
    assert row["workflow_deployment_id"] == deployment_id
    assert row["template_snapshot_json"] is not None
    assert row["template_snapshot_json"] != ""


async def test_take_turn_uses_pinned_snapshot_after_template_removal(monkeypatch) -> None:
    workflow_id, _, _ = await _active_route(monkeypatch)
    result = await agent.start_session(
        "ignored_template", workflow_id, owner_subject="alice"
    )
    async with db.connect() as conn:
        await conn.execute(
            "UPDATE sessions SET template_id = 'removed_template' WHERE id = ?",
            (str(result.session_id),),
        )
        await conn.commit()

    response = await agent.take_turn(result.session_id, "I built a retrying queue.")

    assert response.ai_text
    assert isinstance(response.is_complete, bool)


async def test_start_session_without_workflow_keeps_legacy_template(monkeypatch) -> None:
    monkeypatch.setenv(PRODUCTION_REVISION_ENV, "prod-test-1")
    await db.init_db()

    result = await agent.start_session("backend_engineer", owner_subject="alice")

    row = await db.get_session_row(result.session_id)
    assert result.template_id == "backend_engineer"
    assert result.workflow_id is None
    assert result.workflow_version is None
    assert result.workflow_deployment_id is None
    assert row["workflow_id"] is None
    assert row["template_snapshot_json"] is None


async def test_foreign_tenant_workflow_is_404_before_status_checks(monkeypatch) -> None:
    workflow_id, _, _ = await _active_route(monkeypatch)
    await workflow_store.set_status(workflow_id, status="disabled", actor="alice")

    with pytest.raises(routing.WorkflowRoutingError) as exc:
        await agent.start_session("ignored", workflow_id, owner_subject="bob")

    assert exc.value.status_code == 404
    assert exc.value.reason == "workflow_not_owned"
    async with db.connect() as conn:
        cursor = await conn.execute(
            "SELECT actor_subject, tenant_subject, outcome, target_id, details_json "
            "FROM ai_audit_events WHERE action = 'session.start'"
        )
        event = await cursor.fetchone()
    assert event[0] == "bob"
    assert event[1] == "bob"
    assert event[2] == "denied"
    assert event[3] == str(workflow_id)
    assert "workflow_not_owned" in event[4]


async def test_resolve_active_route_returns_pinned_backend_template(monkeypatch) -> None:
    workflow_id, version, deployment_id = await _active_route(monkeypatch)

    route = await routing.resolve_workflow_route(workflow_id)

    assert route.template.id == "backend_engineer"
    assert route.version == 1
    assert route.content_hash == version.content_hash
    assert route.deployment_id == deployment_id


async def test_resolve_unknown_workflow_is_404(monkeypatch) -> None:
    monkeypatch.setenv(PRODUCTION_REVISION_ENV, "prod-test-1")
    await db.init_db()

    with pytest.raises(routing.WorkflowRoutingError) as exc:
        await routing.resolve_workflow_route(uuid4())

    assert exc.value.status_code == 404


async def test_resolve_disabled_workflow_is_409(monkeypatch) -> None:
    workflow_id, _version, _ = await _active_route(monkeypatch)
    await workflow_store.set_status(workflow_id, status="disabled", actor="alice")

    with pytest.raises(routing.WorkflowRoutingError) as exc:
        await routing.resolve_workflow_route(workflow_id)

    assert exc.value.status_code == 409


async def test_resolve_without_active_deployment_is_409(monkeypatch) -> None:
    workflow_id, _ = await _published_workflow(monkeypatch)

    with pytest.raises(routing.WorkflowRoutingError) as exc:
        await routing.resolve_workflow_route(workflow_id)

    assert exc.value.status_code == 409


async def test_resolve_without_binding_revision_is_409(monkeypatch) -> None:
    workflow_id, _version, _ = await _active_route(monkeypatch)
    monkeypatch.delenv(PRODUCTION_REVISION_ENV)

    with pytest.raises(routing.WorkflowRoutingError) as exc:
        await routing.resolve_workflow_route(workflow_id)

    assert exc.value.status_code == 409


async def test_resolve_with_changed_binding_revision_is_409(monkeypatch) -> None:
    workflow_id, _, _ = await _active_route(monkeypatch)
    monkeypatch.setenv(PRODUCTION_REVISION_ENV, "prod-test-2")

    with pytest.raises(routing.WorkflowRoutingError) as exc:
        await routing.resolve_workflow_route(workflow_id)

    assert exc.value.status_code == 409


async def test_resolve_with_deployment_hash_mismatch_is_409(monkeypatch) -> None:
    workflow_id, version = await _published_workflow(monkeypatch)
    await _insert_deployment(workflow_id, version, content_hash="not-the-hash")

    with pytest.raises(routing.WorkflowRoutingError) as exc:
        await routing.resolve_workflow_route(workflow_id)

    assert exc.value.status_code == 409


async def test_resolve_with_missing_version_is_409(monkeypatch) -> None:
    workflow_id, version = await _published_workflow(monkeypatch)
    await _insert_deployment(workflow_id, version, deployment_version=2)

    with pytest.raises(routing.WorkflowRoutingError) as exc:
        await routing.resolve_workflow_route(workflow_id)

    assert exc.value.status_code == 409


def test_session_template_rejects_corrupt_snapshot() -> None:
    with pytest.raises(
        RuntimeError, match="stored workflow template snapshot is invalid"
    ):
        routing.session_template(
            {"template_id": "rag_architect", "template_snapshot_json": "{"}
        )


def test_session_template_uses_legacy_template_without_snapshot() -> None:
    template = routing.session_template(
        {"template_id": "rag_architect", "template_snapshot_json": None}
    )

    assert template.id == "rag_architect"


async def test_session_template_uses_pinned_snapshot(monkeypatch) -> None:
    _, version = await _published_workflow(monkeypatch)
    snapshot_json = version.artifact.interview_template.model_dump_json()

    template = routing.session_template(
        {
            "template_id": "rag_architect",
            "template_snapshot_json": snapshot_json,
        }
    )

    assert isinstance(template, InterviewTemplate)
    assert template.id == "backend_engineer"
    assert template.nodes == (
        EvidenceNodeSpec("failure_handling", "How partial failures, retries, and idempotency are handled"),
        EvidenceNodeSpec("consistency", "How consistency, ordering, and concurrency are reasoned about"),
        EvidenceNodeSpec("incident_response", "How a production incident is diagnosed and mitigated"),
    )
    assert template == get_template("backend_engineer")
