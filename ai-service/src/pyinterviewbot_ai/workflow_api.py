"""HTTP API for user-owned workflow lifecycle operations."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import workflow_lifecycle as lifecycle
from . import workflow_store as store
from .auth import Principal, require_auth
from .templates import get_template
from .workflow_models import RuntimeRequirements, SecurityPolicy, WorkflowDraft

router = APIRouter(prefix="/workflows", tags=["workflows"])
PrincipalDep = Annotated[Principal, Depends(require_auth)]


class WorkflowUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2_000)
    template_id: str | None = Field(default=None, min_length=1, max_length=120)
    runtime: RuntimeRequirements | None = None
    security: SecurityPolicy | None = None


class TestRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment_id: str = Field(min_length=1)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    test_run_id: str = Field(min_length=1)
    approver_subject: str = Field(min_length=1, max_length=256)
    note: str = Field(default="", max_length=2_000)


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    note: str = Field(default="", max_length=2_000)


class ProductionDeployRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    test_run_id: str = Field(min_length=1)
    approval_id: str = Field(min_length=1)


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment_id: str = Field(min_length=1)


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, store.WorkflowNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, lifecycle.WorkflowPermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (store.WorkflowConflictError, store.WorkflowStateError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="internal workflow error")


def _require_role(principal: Principal, role: str) -> None:
    if not principal.has_role(role):
        raise HTTPException(status_code=403, detail=f"{role} role is required")


def _require_owner(record: store.WorkflowRecord, principal: Principal) -> None:
    if record.owner_subject != principal.subject and not principal.has_role("admin"):
        raise HTTPException(
            status_code=404, detail=f"unknown workflow {record.workflow_id}"
        )


def _workflow_view(record: store.WorkflowRecord) -> dict:
    return {
        "workflow_id": str(record.workflow_id),
        "owner_subject": record.owner_subject,
        "name": record.name,
        "description": record.description,
        "template_id": record.template_id,
        "draft": record.draft.model_dump(mode="json"),
        "status": record.status,
        "latest_version": record.latest_version,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _version_view(record: store.WorkflowVersionRecord) -> dict:
    return {
        "version_id": record.version_id,
        "workflow_id": str(record.workflow_id),
        "version": record.version,
        "content_hash": record.content_hash,
        "artifact": record.artifact.model_dump(mode="json"),
        "created_by": record.created_by,
        "created_at": record.created_at,
    }


def _deployment_view(record: lifecycle.DeploymentRecord) -> dict:
    return {
        "deployment_id": record.deployment_id,
        "workflow_id": str(record.workflow_id),
        "version": record.version,
        "environment": record.environment,
        "content_hash": record.content_hash,
        "binding_fingerprint": record.binding_fingerprint,
        "status": record.status,
        "previous_deployment_id": record.previous_deployment_id,
        "actor_subject": record.actor_subject,
        "test_run_id": record.test_run_id,
        "approval_id": record.approval_id,
        "failure_reason": record.failure_reason,
        "created_at": record.created_at,
        "activated_at": record.activated_at,
    }


def _test_run_view(record: lifecycle.TestRunRecord) -> dict:
    return {
        "test_run_id": record.test_run_id,
        "workflow_id": str(record.workflow_id),
        "version": record.version,
        "content_hash": record.content_hash,
        "environment": record.environment,
        "deployment_id": record.deployment_id,
        "binding_fingerprint": record.binding_fingerprint,
        "status": record.status,
        "evidence": record.evidence,
        "requested_by": record.requested_by,
        "failure_reason": record.failure_reason,
        "created_at": record.created_at,
        "completed_at": record.completed_at,
    }


def _approval_view(record: lifecycle.ApprovalRecord) -> dict:
    return {
        "approval_id": record.approval_id,
        "workflow_id": str(record.workflow_id),
        "version": record.version,
        "content_hash": record.content_hash,
        "test_run_id": record.test_run_id,
        "status": record.status,
        "requested_by": record.requested_by,
        "approver_subject": record.approver_subject,
        "decided_by": record.decided_by,
        "request_note": record.request_note,
        "decision_note": record.decision_note,
        "created_at": record.created_at,
        "decided_at": record.decided_at,
    }


async def _owned_workflow(workflow_id: UUID, principal: Principal) -> store.WorkflowRecord:
    record = await store.get_workflow(workflow_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown workflow {workflow_id}")
    _require_owner(record, principal)
    return record


@router.post("")
async def create_workflow(
    draft: WorkflowDraft,
    principal: PrincipalDep,
) -> dict:
    _require_role(principal, "author")
    name = draft.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name must not be blank")
    try:
        get_template(draft.template_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    normalized = draft.model_copy(update={"name": name})
    try:
        record = await store.create_workflow(
            owner_subject=principal.subject, draft=normalized, actor=principal.subject
        )
    except store.WorkflowError as exc:
        raise _http_error(exc) from exc
    return _workflow_view(record)


@router.get("")
async def list_workflows(principal: PrincipalDep) -> list[dict]:
    include_all = principal.has_role("admin")
    records = await store.list_workflows(
        owner_subject=principal.subject, include_all=include_all
    )
    return [_workflow_view(record) for record in records]


@router.get("/{workflow_id}")
async def get_workflow(
    workflow_id: UUID, principal: PrincipalDep
) -> dict:
    return _workflow_view(await _owned_workflow(workflow_id, principal))


@router.patch("/{workflow_id}")
async def update_workflow(
    workflow_id: UUID,
    update: WorkflowUpdateRequest,
    principal: PrincipalDep,
) -> dict:
    record = await _owned_workflow(workflow_id, principal)
    _require_role(principal, "author")
    changes = update.model_dump(exclude_unset=True)
    if "name" in changes and changes["name"] is not None:
        changes["name"] = changes["name"].strip()
        if not changes["name"]:
            raise HTTPException(status_code=400, detail="name must not be blank")
    template_id = changes.get("template_id")
    if template_id is not None:
        try:
            get_template(template_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    draft = record.draft.model_copy(update=changes)
    try:
        updated = await store.update_draft(
            workflow_id, draft=draft, actor=principal.subject
        )
    except store.WorkflowError as exc:
        raise _http_error(exc) from exc
    return _workflow_view(updated)


@router.post("/{workflow_id}/versions")
async def publish_version(
    workflow_id: UUID,
    principal: PrincipalDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    await _owned_workflow(workflow_id, principal)
    _require_role(principal, "author")
    try:
        record = await store.publish_version(
            workflow_id, actor=principal.subject, idempotency_key=idempotency_key
        )
    except store.WorkflowError as exc:
        raise _http_error(exc) from exc
    return _version_view(record)


@router.get("/{workflow_id}/versions")
async def list_versions(
    workflow_id: UUID, principal: PrincipalDep
) -> list[dict]:
    await _owned_workflow(workflow_id, principal)
    return [_version_view(record) for record in await store.list_versions(workflow_id)]


@router.get("/{workflow_id}/versions/{version}")
async def get_version(
    workflow_id: UUID, version: int, principal: PrincipalDep
) -> dict:
    await _owned_workflow(workflow_id, principal)
    record = await store.get_version(workflow_id, version)
    if record is None:
        raise HTTPException(
            status_code=404, detail=f"unknown workflow version {version}"
        )
    return _version_view(record)


@router.post("/{workflow_id}/versions/{version}/sandbox/deploy")
async def deploy_sandbox(
    workflow_id: UUID,
    version: int,
    principal: PrincipalDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    await _owned_workflow(workflow_id, principal)
    _require_role(principal, "author")
    try:
        record = await lifecycle.deploy_sandbox(
            workflow_id,
            version,
            actor=principal.subject,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:  # normalized below
        raise _http_error(exc) from exc
    return _deployment_view(record)


@router.post("/{workflow_id}/versions/{version}/tests")
async def run_tests(
    workflow_id: UUID,
    version: int,
    request: TestRunRequest,
    principal: PrincipalDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    await _owned_workflow(workflow_id, principal)
    _require_role(principal, "author")
    try:
        record = await lifecycle.run_tests(
            workflow_id,
            version,
            deployment_id=request.deployment_id,
            actor=principal.subject,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        raise _http_error(exc) from exc
    return _test_run_view(record)


@router.get("/{workflow_id}/versions/{version}/tests")
async def list_tests(
    workflow_id: UUID, version: int, principal: PrincipalDep
) -> list[dict]:
    await _owned_workflow(workflow_id, principal)
    return [
        _test_run_view(record)
        for record in await lifecycle.list_test_runs(workflow_id, version)
    ]


@router.post("/{workflow_id}/versions/{version}/approvals")
async def request_approval(
    workflow_id: UUID,
    version: int,
    request: ApprovalRequest,
    principal: PrincipalDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    await _owned_workflow(workflow_id, principal)
    _require_role(principal, "author")
    try:
        record = await lifecycle.request_approval(
            workflow_id,
            version,
            test_run_id=request.test_run_id,
            approver_subject=request.approver_subject,
            actor=principal.subject,
            note=request.note,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        raise _http_error(exc) from exc
    return _approval_view(record)


@router.get("/{workflow_id}/versions/{version}/approvals")
async def list_approvals(
    workflow_id: UUID, version: int, principal: PrincipalDep
) -> list[dict]:
    await _owned_workflow(workflow_id, principal)
    return [
        _approval_view(record)
        for record in await lifecycle.list_approvals(workflow_id, version)
    ]


@router.post(
    "/{workflow_id}/versions/{version}/approvals/{approval_id}/decision"
)
async def decide_approval(
    workflow_id: UUID,
    version: int,
    approval_id: str,
    request: ApprovalDecisionRequest,
    principal: PrincipalDep,
) -> dict:
    _require_role(principal, "approver")
    approval = await lifecycle.get_approval(workflow_id, approval_id)
    if approval is None or approval.version != version:
        raise HTTPException(status_code=404, detail="approval does not match version")
    try:
        record = await lifecycle.decide_approval(
            approval_id,
            actor=principal.subject,
            approve=request.decision == "approve",
            note=request.note,
        )
    except Exception as exc:
        raise _http_error(exc) from exc
    return _approval_view(record)


@router.post("/{workflow_id}/versions/{version}/production/dry-run")
async def production_dry_run(
    workflow_id: UUID,
    version: int,
    request: ProductionDeployRequest,
    principal: PrincipalDep,
) -> dict:
    await _owned_workflow(workflow_id, principal)
    return await lifecycle.deployment_eligibility(
        workflow_id,
        version,
        test_run_id=request.test_run_id,
        approval_id=request.approval_id,
    )


@router.post("/{workflow_id}/versions/{version}/production/deploy")
async def deploy_production(
    workflow_id: UUID,
    version: int,
    request: ProductionDeployRequest,
    principal: PrincipalDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    await _owned_workflow(workflow_id, principal)
    _require_role(principal, "deployer")
    try:
        record = await lifecycle.deploy_production(
            workflow_id,
            version,
            test_run_id=request.test_run_id,
            approval_id=request.approval_id,
            actor=principal.subject,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        raise _http_error(exc) from exc
    return _deployment_view(record)


@router.post("/{workflow_id}/rollback")
async def rollback(
    workflow_id: UUID,
    request: RollbackRequest,
    principal: PrincipalDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    await _owned_workflow(workflow_id, principal)
    _require_role(principal, "deployer")
    try:
        record = await lifecycle.rollback_production(
            workflow_id,
            deployment_id=request.deployment_id,
            actor=principal.subject,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        raise _http_error(exc) from exc
    return _deployment_view(record)


@router.get("/{workflow_id}/deployments")
async def list_deployments(
    workflow_id: UUID, principal: PrincipalDep
) -> list[dict]:
    await _owned_workflow(workflow_id, principal)
    return [
        _deployment_view(record)
        for record in await lifecycle.list_deployments(workflow_id)
    ]


@router.get("/{workflow_id}/audit")
async def list_audit(
    workflow_id: UUID, principal: PrincipalDep
) -> list[dict]:
    await _owned_workflow(workflow_id, principal)
    return await store.list_audit(workflow_id)


@router.post("/{workflow_id}/disable")
async def disable_workflow(
    workflow_id: UUID, principal: PrincipalDep
) -> dict:
    await _owned_workflow(workflow_id, principal)
    return _workflow_view(
        await store.set_status(workflow_id, status="disabled", actor=principal.subject)
    )


@router.post("/{workflow_id}/enable")
async def enable_workflow(
    workflow_id: UUID, principal: PrincipalDep
) -> dict:
    await _owned_workflow(workflow_id, principal)
    return _workflow_view(
        await store.set_status(workflow_id, status="active", actor=principal.subject)
    )


__all__ = ["router"]
