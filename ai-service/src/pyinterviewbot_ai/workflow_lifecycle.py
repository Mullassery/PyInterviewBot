"""Sandbox testing, approval, production promotion, and rollback."""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID, uuid4

import aiosqlite

from . import db
from .workflow_env import (
    SANDBOX,
    BindingError,
    production_binding,
    sandbox_binding,
)
from .workflow_models import artifact_from_json
from .workflow_store import (
    WorkflowConflictError,
    WorkflowNotFoundError,
    WorkflowStateError,
    _audit,
    _clean_key,
    _now,
)
from .workflow_tests import run_mandatory_suites


class WorkflowPermissionError(Exception):
    pass


@dataclass(frozen=True)
class DeploymentRecord:
    deployment_id: str
    workflow_id: UUID
    version: int
    environment: str
    content_hash: str
    binding_fingerprint: str
    status: str
    previous_deployment_id: str | None
    actor_subject: str
    test_run_id: str | None
    approval_id: str | None
    idempotency_key: str | None
    failure_reason: str | None
    created_at: str
    activated_at: str | None


@dataclass(frozen=True)
class TestRunRecord:
    test_run_id: str
    workflow_id: UUID
    version: int
    content_hash: str
    environment: str
    deployment_id: str
    binding_fingerprint: str
    status: str
    evidence: list[dict]
    requested_by: str
    idempotency_key: str | None
    failure_reason: str | None
    created_at: str
    completed_at: str | None


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    workflow_id: UUID
    version: int
    content_hash: str
    test_run_id: str
    status: str
    requested_by: str
    approver_subject: str
    decided_by: str | None
    request_note: str
    decision_note: str
    idempotency_key: str | None
    created_at: str
    decided_at: str | None


def _deployment(row: aiosqlite.Row) -> DeploymentRecord:
    return DeploymentRecord(
        deployment_id=row["id"],
        workflow_id=UUID(row["workflow_id"]),
        version=row["version"],
        environment=row["environment"],
        content_hash=row["content_hash"],
        binding_fingerprint=row["binding_fingerprint"],
        status=row["status"],
        previous_deployment_id=row["previous_deployment_id"],
        actor_subject=row["actor_subject"],
        test_run_id=row["test_run_id"],
        approval_id=row["approval_id"],
        idempotency_key=row["idempotency_key"],
        failure_reason=row["failure_reason"],
        created_at=row["created_at"],
        activated_at=row["activated_at"],
    )


def _test_run(row: aiosqlite.Row) -> TestRunRecord:
    return TestRunRecord(
        test_run_id=row["id"],
        workflow_id=UUID(row["workflow_id"]),
        version=row["version"],
        content_hash=row["content_hash"],
        environment=row["environment"],
        deployment_id=row["deployment_id"],
        binding_fingerprint=row["binding_fingerprint"],
        status=row["status"],
        evidence=json.loads(row["evidence_json"]),
        requested_by=row["requested_by"],
        idempotency_key=row["idempotency_key"],
        failure_reason=row["failure_reason"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


def _approval(row: aiosqlite.Row) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=row["id"],
        workflow_id=UUID(row["workflow_id"]),
        version=row["version"],
        content_hash=row["content_hash"],
        test_run_id=row["test_run_id"],
        status=row["status"],
        requested_by=row["requested_by"],
        approver_subject=row["approver_subject"],
        decided_by=row["decided_by"],
        request_note=row["request_note"],
        decision_note=row["decision_note"],
        idempotency_key=row["idempotency_key"],
        created_at=row["created_at"],
        decided_at=row["decided_at"],
    )


def _deployment_idempotency_key(operation: str, key: str | None) -> str | None:
    return f"{operation}:{key}" if key is not None else None


async def _ensure_active_workflow(
    conn: aiosqlite.Connection, workflow_id: UUID | str
) -> None:
    cursor = await conn.execute(
        "SELECT status FROM workflows WHERE id = ?", (str(workflow_id),)
    )
    workflow = await cursor.fetchone()
    if workflow is None:
        await conn.rollback()
        raise WorkflowNotFoundError(f"unknown workflow {workflow_id}")
    if workflow["status"] != "active":
        await conn.rollback()
        raise WorkflowStateError("workflow is disabled")


def _replay_production_deployment(
    existing: DeploymentRecord, version: int
) -> DeploymentRecord:
    if existing.environment != "production":
        raise WorkflowConflictError("idempotency key belongs to another environment")
    if existing.status == "failed":
        raise WorkflowStateError(
            existing.failure_reason or "production deployment previously failed"
        )
    if existing.version != version:
        raise WorkflowConflictError(
            "idempotency key was used for a different workflow version"
        )
    return existing


async def _existing_deployment(
    conn: aiosqlite.Connection, workflow_id: UUID, key: str | None
) -> DeploymentRecord | None:
    if key is None:
        return None
    cursor = await conn.execute(
        "SELECT * FROM workflow_deployments WHERE workflow_id = ? AND idempotency_key = ?",
        (str(workflow_id), key),
    )
    row = await cursor.fetchone()
    return _deployment(row) if row is not None else None


async def _existing_test_run(
    conn: aiosqlite.Connection, workflow_id: UUID, key: str | None
) -> TestRunRecord | None:
    if key is None:
        return None
    cursor = await conn.execute(
        "SELECT * FROM workflow_test_runs WHERE workflow_id = ? AND idempotency_key = ?",
        (str(workflow_id), key),
    )
    row = await cursor.fetchone()
    return _test_run(row) if row is not None else None


async def _existing_approval(
    conn: aiosqlite.Connection, workflow_id: UUID, key: str | None
) -> ApprovalRecord | None:
    if key is None:
        return None
    cursor = await conn.execute(
        "SELECT * FROM workflow_approvals WHERE workflow_id = ? AND idempotency_key = ?",
        (str(workflow_id), key),
    )
    row = await cursor.fetchone()
    return _approval(row) if row is not None else None


async def deploy_sandbox(
    workflow_id: UUID,
    version: int,
    *,
    actor: str,
    idempotency_key: str | None = None,
) -> DeploymentRecord:
    key = _deployment_idempotency_key("sandbox_deploy", _clean_key(idempotency_key))
    async with db.connect() as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("BEGIN IMMEDIATE")
        existing = await _existing_deployment(conn, workflow_id, key)
        if existing is not None:
            await conn.rollback()
            if existing.environment != SANDBOX:
                raise WorkflowConflictError("idempotency key belongs to another environment")
            if existing.version != version:
                raise WorkflowConflictError(
                    "idempotency key was used for a different workflow version"
                )
            return existing

        cursor = await conn.execute("SELECT * FROM workflows WHERE id = ?", (str(workflow_id),))
        workflow = await cursor.fetchone()
        if workflow is None:
            await conn.rollback()
            raise WorkflowNotFoundError(f"unknown workflow {workflow_id}")
        if workflow["status"] != "active":
            await conn.rollback()
            raise WorkflowStateError("workflow is disabled")

        cursor = await conn.execute(
            "SELECT * FROM workflow_versions WHERE workflow_id = ? AND version = ?",
            (str(workflow_id), version),
        )
        version_row = await cursor.fetchone()
        if version_row is None:
            await conn.rollback()
            raise WorkflowNotFoundError(f"unknown workflow version {version}")

        artifact = artifact_from_json(version_row["artifact_json"])
        binding = sandbox_binding()
        if not binding.supports(artifact.runtime):
            await conn.rollback()
            raise WorkflowStateError("sandbox binding lacks required capabilities")

        cursor = await conn.execute(
            "SELECT * FROM workflow_deployments "
            "WHERE workflow_id = ? AND environment = ? AND status = 'active' "
            "ORDER BY created_at DESC LIMIT 1",
            (str(workflow_id), SANDBOX),
        )
        current = await cursor.fetchone()
        previous_id = current["id"] if current is not None else None
        if current is not None:
            await conn.execute(
                "UPDATE workflow_deployments SET status = 'superseded' WHERE id = ?",
                (current["id"],),
            )

        deployment_id = str(uuid4())
        now = _now()
        await conn.execute(
            "INSERT INTO workflow_deployments "
            "(id, workflow_id, version, environment, content_hash, binding_fingerprint, "
            "status, previous_deployment_id, actor_subject, test_run_id, approval_id, "
            "idempotency_key, failure_reason, created_at, activated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL, NULL, ?, NULL, ?, ?)",
            (
                deployment_id,
                str(workflow_id),
                version,
                SANDBOX,
                version_row["content_hash"],
                binding.fingerprint,
                previous_id,
                actor,
                key,
                now,
                now,
            ),
        )
        await _audit(
            conn,
            workflow_id=workflow_id,
            version=version,
            action="deployment.sandbox.active",
            actor=actor,
            details={
                "deployment_id": deployment_id,
                "content_hash": version_row["content_hash"],
                "binding_fingerprint": binding.fingerprint,
            },
        )
        await conn.commit()
    return DeploymentRecord(
        deployment_id=deployment_id,
        workflow_id=workflow_id,
        version=version,
        environment=SANDBOX,
        content_hash=version_row["content_hash"],
        binding_fingerprint=binding.fingerprint,
        status="active",
        previous_deployment_id=previous_id,
        actor_subject=actor,
        test_run_id=None,
        approval_id=None,
        idempotency_key=key,
        failure_reason=None,
        created_at=now,
        activated_at=now,
    )


async def run_tests(
    workflow_id: UUID,
    version: int,
    *,
    deployment_id: str,
    actor: str,
    idempotency_key: str | None = None,
) -> TestRunRecord:
    key = _clean_key(idempotency_key)
    async with db.connect() as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("BEGIN IMMEDIATE")
        existing = await _existing_test_run(conn, workflow_id, key)
        if existing is not None:
            await conn.rollback()
            return existing

        cursor = await conn.execute(
            "SELECT * FROM workflow_versions WHERE workflow_id = ? AND version = ?",
            (str(workflow_id), version),
        )
        version_row = await cursor.fetchone()
        if version_row is None:
            await conn.rollback()
            raise WorkflowNotFoundError(f"unknown workflow version {version}")

        cursor = await conn.execute(
            "SELECT * FROM workflow_deployments WHERE id = ? AND workflow_id = ?",
            (deployment_id, str(workflow_id)),
        )
        deployment_row = await cursor.fetchone()
        if deployment_row is None:
            await conn.rollback()
            raise WorkflowNotFoundError(f"unknown sandbox deployment {deployment_id}")
        if deployment_row["environment"] != SANDBOX:
            await conn.rollback()
            raise WorkflowStateError("tests require a sandbox deployment")
        if deployment_row["status"] != "active":
            await conn.rollback()
            raise WorkflowStateError("sandbox deployment is not active")
        if deployment_row["content_hash"] != version_row["content_hash"]:
            await conn.rollback()
            raise WorkflowStateError("sandbox deployment hash does not match artifact")

        binding = sandbox_binding()
        if deployment_row["binding_fingerprint"] != binding.fingerprint:
            await conn.rollback()
            raise WorkflowStateError("sandbox binding changed since deployment")

        artifact = artifact_from_json(version_row["artifact_json"])
        evidence = await run_mandatory_suites(artifact, binding)
        passed = all(item["passed"] for item in evidence)
        status = "passed" if passed else "failed"
        failure_reason = None if passed else "one or more mandatory suites failed"
        test_run_id = str(uuid4())
        now = _now()
        await conn.execute(
            "INSERT INTO workflow_test_runs "
            "(id, workflow_id, version, content_hash, environment, deployment_id, "
            "binding_fingerprint, status, evidence_json, requested_by, idempotency_key, "
            "failure_reason, created_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                test_run_id,
                str(workflow_id),
                version,
                version_row["content_hash"],
                SANDBOX,
                deployment_id,
                binding.fingerprint,
                status,
                json.dumps(evidence),
                actor,
                key,
                failure_reason,
                now,
                now,
            ),
        )
        await _audit(
            conn,
            workflow_id=workflow_id,
            version=version,
            action=f"test.{status}",
            actor=actor,
            details={
                "test_run_id": test_run_id,
                "deployment_id": deployment_id,
                "content_hash": version_row["content_hash"],
            },
        )
        await conn.commit()
    return TestRunRecord(
        test_run_id=test_run_id,
        workflow_id=workflow_id,
        version=version,
        content_hash=version_row["content_hash"],
        environment=SANDBOX,
        deployment_id=deployment_id,
        binding_fingerprint=binding.fingerprint,
        status=status,
        evidence=evidence,
        requested_by=actor,
        idempotency_key=key,
        failure_reason=failure_reason,
        created_at=now,
        completed_at=now,
    )


def _approval_test_error(test_row, version_row, version: int) -> str | None:
    if test_row is None:
        return "unknown test run"
    if test_row["status"] != "passed":
        return "approval requires a passing sandbox test run"
    if test_row["content_hash"] != version_row["content_hash"]:
        return "test run is for a different artifact hash"
    if test_row["version"] != version:
        return "test run is for a different artifact version"
    return None


async def request_approval(
    workflow_id: UUID,
    version: int,
    *,
    test_run_id: str,
    approver_subject: str,
    actor: str,
    note: str = "",
    idempotency_key: str | None = None,
) -> ApprovalRecord:
    key = _clean_key(idempotency_key)
    approver_subject = approver_subject.strip()
    if not approver_subject:
        raise WorkflowStateError("approver_subject is required")
    if approver_subject == actor:
        raise WorkflowStateError("requester cannot approve their own deployment")
    async with db.connect() as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("BEGIN IMMEDIATE")
        existing = await _existing_approval(conn, workflow_id, key)
        if existing is not None:
            await conn.rollback()
            return existing

        cursor = await conn.execute("SELECT * FROM workflows WHERE id = ?", (str(workflow_id),))
        workflow = await cursor.fetchone()
        if workflow is None:
            await conn.rollback()
            raise WorkflowNotFoundError(f"unknown workflow {workflow_id}")
        if workflow["status"] != "active":
            await conn.rollback()
            raise WorkflowStateError("workflow is disabled")

        cursor = await conn.execute(
            "SELECT * FROM workflow_versions WHERE workflow_id = ? AND version = ?",
            (str(workflow_id), version),
        )
        version_row = await cursor.fetchone()
        if version_row is None:
            await conn.rollback()
            raise WorkflowNotFoundError(f"unknown workflow version {version}")

        cursor = await conn.execute(
            "SELECT * FROM workflow_test_runs WHERE id = ? AND workflow_id = ?",
            (test_run_id, str(workflow_id)),
        )
        test_row = await cursor.fetchone()
        error = _approval_test_error(test_row, version_row, version)
        if error is not None:
            await conn.rollback()
            if test_row is None:
                raise WorkflowNotFoundError(f"unknown test run {test_run_id}")
            raise WorkflowStateError(error)

        approval_id = str(uuid4())
        now = _now()
        await conn.execute(
            "INSERT INTO workflow_approvals "
            "(id, workflow_id, version, content_hash, test_run_id, status, requested_by, "
            "approver_subject, decided_by, request_note, decision_note, idempotency_key, "
            "created_at, decided_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, NULL, ?, '', ?, ?, NULL)",
            (
                approval_id,
                str(workflow_id),
                version,
                version_row["content_hash"],
                test_run_id,
                actor,
                approver_subject,
                note,
                key,
                now,
            ),
        )
        await _audit(
            conn,
            workflow_id=workflow_id,
            version=version,
            action="approval.requested",
            actor=actor,
            details={
                "approval_id": approval_id,
                "approver_subject": approver_subject,
                "test_run_id": test_run_id,
                "content_hash": version_row["content_hash"],
            },
        )
        await conn.commit()
    return ApprovalRecord(
        approval_id=approval_id,
        workflow_id=workflow_id,
        version=version,
        content_hash=version_row["content_hash"],
        test_run_id=test_run_id,
        status="pending",
        requested_by=actor,
        approver_subject=approver_subject,
        decided_by=None,
        request_note=note,
        decision_note="",
        idempotency_key=key,
        created_at=now,
        decided_at=None,
    )


async def decide_approval(
    approval_id: str,
    *,
    actor: str,
    approve: bool,
    note: str = "",
) -> ApprovalRecord:
    async with db.connect() as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("BEGIN IMMEDIATE")
        cursor = await conn.execute(
            "SELECT * FROM workflow_approvals WHERE id = ?", (approval_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            await conn.rollback()
            raise WorkflowNotFoundError(f"unknown approval {approval_id}")
        if row["approver_subject"] != actor:
            await conn.rollback()
            raise WorkflowPermissionError("only the assigned approver may decide")
        await _ensure_active_workflow(conn, row["workflow_id"])
        if row["status"] != "pending":
            await conn.rollback()
            raise WorkflowStateError("approval has already been decided")
        if approve:
            cursor = await conn.execute(
                "SELECT * FROM workflow_test_runs WHERE id = ?", (row["test_run_id"],)
            )
            test_row = await cursor.fetchone()
            if test_row is None or test_row["status"] != "passed":
                await conn.rollback()
                raise WorkflowStateError("cannot approve without a passing test run")
            if test_row["content_hash"] != row["content_hash"]:
                await conn.rollback()
                raise WorkflowStateError("approval hash no longer matches its test run")
        status = "approved" if approve else "rejected"
        now = _now()
        await conn.execute(
            "UPDATE workflow_approvals SET status = ?, decided_by = ?, "
            "decision_note = ?, decided_at = ? WHERE id = ?",
            (status, actor, note, now, approval_id),
        )
        await _audit(
            conn,
            workflow_id=UUID(row["workflow_id"]),
            version=row["version"],
            action=f"approval.{status}",
            actor=actor,
            details={"approval_id": approval_id, "content_hash": row["content_hash"]},
        )
        await conn.commit()
    return ApprovalRecord(
        approval_id=approval_id,
        workflow_id=UUID(row["workflow_id"]),
        version=row["version"],
        content_hash=row["content_hash"],
        test_run_id=row["test_run_id"],
        status=status,
        requested_by=row["requested_by"],
        approver_subject=row["approver_subject"],
        decided_by=actor,
        request_note=row["request_note"],
        decision_note=note,
        idempotency_key=row["idempotency_key"],
        created_at=row["created_at"],
        decided_at=now,
    )


def _test_checks(test_row, version_row) -> list[str]:
    if test_row is None:
        return ["passing sandbox test run is required"]
    errors: list[str] = []
    if test_row["status"] != "passed":
        errors.append("latest test run did not pass")
    if test_row["content_hash"] != version_row["content_hash"]:
        errors.append("test run hash does not match the artifact")
    return errors


def _approval_checks(approval_row, test_row, version_row) -> list[str]:
    if approval_row is None:
        return ["approved promotion is required"]
    errors: list[str] = []
    if approval_row["status"] != "approved":
        errors.append("promotion approval is not approved")
    if approval_row["content_hash"] != version_row["content_hash"]:
        errors.append("approval hash does not match the artifact")
    if test_row is not None and approval_row["test_run_id"] != test_row["id"]:
        errors.append("approval is not tied to this test run")
    return errors


def _sandbox_checks(sandbox_row, test_row, version_row) -> list[str]:
    if sandbox_row is None:
        return ["sandbox deployment is required"]
    errors: list[str] = []
    if sandbox_row["content_hash"] != version_row["content_hash"]:
        errors.append("sandbox deployment hash does not match the artifact")
    if sandbox_row["status"] == "failed":
        errors.append("sandbox deployment failed")
    if test_row is not None and sandbox_row["id"] != test_row["deployment_id"]:
        errors.append("test run is not tied to this sandbox deployment")
    return errors


def _binding_checks(binding, artifact) -> list[str]:
    if binding is None:
        return ["production binding is unavailable"]
    if not binding.supports(artifact.runtime):
        return ["production binding lacks required capabilities"]
    return []


def _production_checks(
    *,
    workflow_row: aiosqlite.Row | None,
    version_row: aiosqlite.Row | None,
    test_row: aiosqlite.Row | None,
    approval_row: aiosqlite.Row | None,
    sandbox_row: aiosqlite.Row | None,
    binding,
) -> list[str]:
    if workflow_row is None:
        return ["workflow does not exist"]
    errors = []
    if workflow_row["status"] != "active":
        errors.append("workflow is disabled")
    if version_row is None:
        return [*errors, "workflow version does not exist"]
    artifact = artifact_from_json(version_row["artifact_json"])
    if not artifact.verify_content_hash():
        errors.append("artifact hash is invalid")
    errors.extend(_test_checks(test_row, version_row))
    errors.extend(_approval_checks(approval_row, test_row, version_row))
    errors.extend(_sandbox_checks(sandbox_row, test_row, version_row))
    errors.extend(_binding_checks(binding, artifact))
    return errors


async def deployment_eligibility(
    workflow_id: UUID,
    version: int,
    *,
    test_run_id: str,
    approval_id: str,
) -> dict:
    async with db.connect() as conn:
        conn.row_factory = aiosqlite.Row
        workflow_row = await (
            await conn.execute("SELECT * FROM workflows WHERE id = ?", (str(workflow_id),))
        ).fetchone()
        version_row = await (
            await conn.execute(
                "SELECT * FROM workflow_versions WHERE workflow_id = ? AND version = ?",
                (str(workflow_id), version),
            )
        ).fetchone()
        test_row = await (
            await conn.execute(
                "SELECT * FROM workflow_test_runs WHERE id = ? AND workflow_id = ?",
                (test_run_id, str(workflow_id)),
            )
        ).fetchone()
        approval_row = await (
            await conn.execute(
                "SELECT * FROM workflow_approvals WHERE id = ? AND workflow_id = ?",
                (approval_id, str(workflow_id)),
            )
        ).fetchone()
        sandbox_row = await (
            await conn.execute(
                "SELECT * FROM workflow_deployments "
                "WHERE workflow_id = ? AND version = ? AND environment = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (str(workflow_id), version, SANDBOX),
            )
        ).fetchone()
        try:
            binding = production_binding()
            binding_error = None
        except BindingError as exc:
            binding = None
            binding_error = str(exc)
        errors = _production_checks(
            workflow_row=workflow_row,
            version_row=version_row,
            test_row=test_row,
            approval_row=approval_row,
            sandbox_row=sandbox_row,
            binding=binding,
        )
        if binding_error:
            errors.append(binding_error)
    return {
        "eligible": not errors,
        "errors": errors,
        "content_hash": version_row["content_hash"] if version_row else None,
        "binding": binding.safe_view() if binding is not None else None,
    }


async def deploy_production(
    workflow_id: UUID,
    version: int,
    *,
    test_run_id: str,
    approval_id: str,
    actor: str,
    idempotency_key: str | None = None,
) -> DeploymentRecord:
    key = _deployment_idempotency_key(
        "production_deploy", _clean_key(idempotency_key)
    )
    async with db.connect() as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("BEGIN IMMEDIATE")
        existing = await _existing_deployment(conn, workflow_id, key)
        if existing is not None:
            await conn.rollback()
            return _replay_production_deployment(existing, version)

        workflow_row = await (
            await conn.execute("SELECT * FROM workflows WHERE id = ?", (str(workflow_id),))
        ).fetchone()
        version_row = await (
            await conn.execute(
                "SELECT * FROM workflow_versions WHERE workflow_id = ? AND version = ?",
                (str(workflow_id), version),
            )
        ).fetchone()
        test_row = await (
            await conn.execute(
                "SELECT * FROM workflow_test_runs WHERE id = ? AND workflow_id = ?",
                (test_run_id, str(workflow_id)),
            )
        ).fetchone()
        approval_row = await (
            await conn.execute(
                "SELECT * FROM workflow_approvals WHERE id = ? AND workflow_id = ?",
                (approval_id, str(workflow_id)),
            )
        ).fetchone()
        sandbox_row = await (
            await conn.execute(
                "SELECT * FROM workflow_deployments "
                "WHERE workflow_id = ? AND version = ? AND environment = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (str(workflow_id), version, SANDBOX),
            )
        ).fetchone()
        if version_row is None:
            await conn.rollback()
            raise WorkflowNotFoundError(f"unknown workflow version {version}")
        binding_error: str | None = None
        binding = None
        try:
            binding = production_binding()
        except BindingError as exc:
            binding_error = str(exc)

        errors = _production_checks(
            workflow_row=workflow_row,
            version_row=version_row,
            test_row=test_row,
            approval_row=approval_row,
            sandbox_row=sandbox_row,
            binding=binding,
        )
        if binding_error and binding_error not in errors:
            errors.append(binding_error)

        current = await (
            await conn.execute(
                "SELECT * FROM workflow_deployments "
                "WHERE workflow_id = ? AND environment = 'production' AND status = 'active' "
                "ORDER BY created_at DESC LIMIT 1",
                (str(workflow_id),),
            )
        ).fetchone()
        previous_id = current["id"] if current is not None else None
        if errors:
            failed_id = str(uuid4())
            failed_at = _now()
            reason = "; ".join(errors)
            await conn.execute(
                "INSERT INTO workflow_deployments "
                "(id, workflow_id, version, environment, content_hash, binding_fingerprint, "
                "status, previous_deployment_id, actor_subject, test_run_id, approval_id, "
                "idempotency_key, failure_reason, created_at, activated_at) "
                "VALUES (?, ?, ?, 'production', ?, ?, 'failed', ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    failed_id,
                    str(workflow_id),
                    version,
                    version_row["content_hash"],
                    binding.fingerprint if binding is not None else "",
                    previous_id,
                    actor,
                    test_run_id,
                    approval_id,
                    key,
                    reason,
                    failed_at,
                ),
            )
            await _audit(
                conn,
                workflow_id=workflow_id,
                version=version,
                action="deployment.production.failed",
                actor=actor,
                details={
                    "deployment_id": failed_id,
                    "content_hash": version_row["content_hash"],
                    "reason": reason,
                },
            )
            await conn.commit()
            raise WorkflowStateError(reason)
        if current is not None:
            if (
                current["version"] == version
                and current["content_hash"] == version_row["content_hash"]
                and current["binding_fingerprint"] == binding.fingerprint
            ):
                await conn.rollback()
                return _deployment(current)
            await conn.execute(
                "UPDATE workflow_deployments SET status = 'superseded' WHERE id = ?",
                (current["id"],),
            )

        deployment_id = str(uuid4())
        now = _now()
        await conn.execute(
            "INSERT INTO workflow_deployments "
            "(id, workflow_id, version, environment, content_hash, binding_fingerprint, "
            "status, previous_deployment_id, actor_subject, test_run_id, approval_id, "
            "idempotency_key, failure_reason, created_at, activated_at) "
            "VALUES (?, ?, ?, 'production', ?, ?, 'active', ?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                deployment_id,
                str(workflow_id),
                version,
                version_row["content_hash"],
                binding.fingerprint,
                previous_id,
                actor,
                test_run_id,
                approval_id,
                key,
                now,
                now,
            ),
        )
        await _audit(
            conn,
            workflow_id=workflow_id,
            version=version,
            action="deployment.production.active",
            actor=actor,
            details={
                "deployment_id": deployment_id,
                "content_hash": version_row["content_hash"],
                "binding_fingerprint": binding.fingerprint,
                "test_run_id": test_run_id,
                "approval_id": approval_id,
            },
        )
        await conn.commit()
    return DeploymentRecord(
        deployment_id=deployment_id,
        workflow_id=workflow_id,
        version=version,
        environment="production",
        content_hash=version_row["content_hash"],
        binding_fingerprint=binding.fingerprint,
        status="active",
        previous_deployment_id=previous_id,
        actor_subject=actor,
        test_run_id=test_run_id,
        approval_id=approval_id,
        idempotency_key=key,
        failure_reason=None,
        created_at=now,
        activated_at=now,
    )


async def rollback_production(
    workflow_id: UUID,
    *,
    deployment_id: str,
    actor: str,
    idempotency_key: str | None = None,
) -> DeploymentRecord:
    key = _deployment_idempotency_key(
        "production_rollback", _clean_key(idempotency_key)
    )
    async with db.connect() as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("BEGIN IMMEDIATE")
        existing = await _existing_deployment(conn, workflow_id, key)
        if existing is not None:
            await conn.rollback()
            return existing
        await _ensure_active_workflow(conn, workflow_id)

        cursor = await conn.execute(
            "SELECT * FROM workflow_deployments WHERE id = ? AND workflow_id = ?",
            (deployment_id, str(workflow_id)),
        )
        target = await cursor.fetchone()
        if target is None:
            await conn.rollback()
            raise WorkflowNotFoundError(f"unknown deployment {deployment_id}")
        if target["environment"] != "production":
            await conn.rollback()
            raise WorkflowStateError("only production deployments can be rolled back to")
        if target["status"] == "failed":
            await conn.rollback()
            raise WorkflowStateError("cannot roll back to a failed deployment")
        try:
            binding = production_binding()
        except BindingError as exc:
            await conn.rollback()
            raise WorkflowStateError(str(exc)) from exc
        if target["binding_fingerprint"] != binding.fingerprint:
            await conn.rollback()
            raise WorkflowStateError(
                "target deployment was tested against a different environment binding"
            )

        current = await (
            await conn.execute(
                "SELECT * FROM workflow_deployments "
                "WHERE workflow_id = ? AND environment = 'production' AND status = 'active' "
                "ORDER BY created_at DESC LIMIT 1",
                (str(workflow_id),),
            )
        ).fetchone()
        if current is not None and current["id"] == deployment_id:
            await conn.rollback()
            return _deployment(current)
        previous_id = current["id"] if current is not None else None
        if current is not None:
            await conn.execute(
                "UPDATE workflow_deployments SET status = 'superseded' WHERE id = ?",
                (current["id"],),
            )
        new_id = str(uuid4())
        now = _now()
        await conn.execute(
            "INSERT INTO workflow_deployments "
            "(id, workflow_id, version, environment, content_hash, binding_fingerprint, "
            "status, previous_deployment_id, actor_subject, test_run_id, approval_id, "
            "idempotency_key, failure_reason, created_at, activated_at) "
            "VALUES (?, ?, ?, 'production', ?, ?, 'active', ?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                new_id,
                str(workflow_id),
                target["version"],
                target["content_hash"],
                target["binding_fingerprint"],
                previous_id,
                actor,
                target["test_run_id"],
                target["approval_id"],
                key,
                now,
                now,
            ),
        )
        await _audit(
            conn,
            workflow_id=workflow_id,
            version=target["version"],
            action="deployment.production.rollback",
            actor=actor,
            details={
                "deployment_id": new_id,
                "rolled_back_from": previous_id,
                "target_deployment_id": deployment_id,
                "content_hash": target["content_hash"],
            },
        )
        await conn.commit()
    return DeploymentRecord(
        deployment_id=new_id,
        workflow_id=workflow_id,
        version=target["version"],
        environment="production",
        content_hash=target["content_hash"],
        binding_fingerprint=target["binding_fingerprint"],
        status="active",
        previous_deployment_id=previous_id,
        actor_subject=actor,
        test_run_id=target["test_run_id"],
        approval_id=target["approval_id"],
        idempotency_key=key,
        failure_reason=None,
        created_at=now,
        activated_at=now,
    )


async def list_deployments(workflow_id: UUID) -> list[DeploymentRecord]:
    async with db.connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM workflow_deployments WHERE workflow_id = ? "
            "ORDER BY created_at ASC, rowid ASC",
            (str(workflow_id),),
        )
        return [_deployment(row) for row in await cursor.fetchall()]


async def get_test_run(workflow_id: UUID, test_run_id: str) -> TestRunRecord | None:
    async with db.connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM workflow_test_runs WHERE id = ? AND workflow_id = ?",
            (test_run_id, str(workflow_id)),
        )
        row = await cursor.fetchone()
        return _test_run(row) if row is not None else None


async def list_test_runs(workflow_id: UUID, version: int) -> list[TestRunRecord]:
    async with db.connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM workflow_test_runs WHERE workflow_id = ? AND version = ? "
            "ORDER BY created_at ASC, rowid ASC",
            (str(workflow_id), version),
        )
        return [_test_run(row) for row in await cursor.fetchall()]


async def get_approval(workflow_id: UUID, approval_id: str) -> ApprovalRecord | None:
    async with db.connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM workflow_approvals WHERE id = ? AND workflow_id = ?",
            (approval_id, str(workflow_id)),
        )
        row = await cursor.fetchone()
        return _approval(row) if row is not None else None


async def list_approvals(workflow_id: UUID, version: int) -> list[ApprovalRecord]:
    async with db.connect() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM workflow_approvals WHERE workflow_id = ? AND version = ? "
            "ORDER BY created_at ASC, rowid ASC",
            (str(workflow_id), version),
        )
        return [_approval(row) for row in await cursor.fetchall()]


__all__ = [
    "ApprovalRecord",
    "DeploymentRecord",
    "TestRunRecord",
    "WorkflowPermissionError",
    "decide_approval",
    "deploy_production",
    "deploy_sandbox",
    "deployment_eligibility",
    "get_approval",
    "get_test_run",
    "list_approvals",
    "list_deployments",
    "list_test_runs",
    "request_approval",
    "rollback_production",
    "run_tests",
]
