"""Resolve sessions to immutable, active production workflow routes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from pydantic import ValidationError

from . import workflow_lifecycle, workflow_store
from .providers.base import EvidenceNodeSpec
from .templates import InterviewTemplate, get_template
from .workflow_env import BindingError, production_binding
from .workflow_models import TemplateSnapshot
from .workflow_store import WorkflowStateError


class WorkflowRoutingError(Exception):
    """A session cannot be routed to an active production workflow."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 409,
        reason: str | None = None,
    ) -> None:
        """Record the client-facing status for this routing failure."""
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


@dataclass(frozen=True)
class WorkflowRoute:
    workflow_id: UUID
    deployment_id: str
    version: int
    content_hash: str
    template: InterviewTemplate
    template_snapshot_json: str
    owner_subject: str


def _template_from_snapshot(snapshot: TemplateSnapshot) -> InterviewTemplate:
    return InterviewTemplate(
        id=snapshot.id,
        title=snapshot.title,
        role=snapshot.role,
        competency=snapshot.competency,
        description=snapshot.description,
        nodes=tuple(
            EvidenceNodeSpec(node.id, node.description) for node in snapshot.nodes
        ),
        question_bank=snapshot.question_bank,
        max_turns=snapshot.max_turns,
        system_prompt=snapshot.system_prompt,
        evaluation_notes=snapshot.evaluation_notes,
        version=snapshot.version,
    )


async def resolve_workflow_route(
    workflow_id: UUID,
    *,
    tenant_subject: str | None = None,
    allow_cross_tenant: bool = False,
) -> WorkflowRoute:
    workflow = await workflow_store.get_workflow(workflow_id)
    if workflow is None:
        raise WorkflowRoutingError(
            f"unknown workflow {workflow_id}", status_code=404
        )
    if (
        tenant_subject is not None
        and workflow.owner_subject != tenant_subject
        and not allow_cross_tenant
    ):
        raise WorkflowRoutingError(
            f"unknown workflow {workflow_id}",
            status_code=404,
            reason="workflow_not_owned",
        )
    if workflow.status != "active":
        raise WorkflowRoutingError("workflow is not active")

    deployment = await workflow_lifecycle.get_active_production_deployment(workflow_id)
    if deployment is None:
        raise WorkflowRoutingError("workflow has no active production deployment")

    try:
        binding = production_binding()
    except BindingError as exc:
        raise WorkflowRoutingError("production binding is not configured") from exc
    if deployment.binding_fingerprint != binding.fingerprint:
        raise WorkflowRoutingError(
            "production binding does not match the active deployment"
        )

    try:
        version = await workflow_store.get_version(workflow_id, deployment.version)
    except WorkflowStateError as exc:
        raise WorkflowRoutingError(
            "stored workflow version failed integrity verification"
        ) from exc
    if version is None:
        raise WorkflowRoutingError("deployed workflow version is missing")
    if deployment.content_hash != version.content_hash:
        raise WorkflowRoutingError(
            "deployment content hash does not match the workflow version"
        )

    snapshot = version.artifact.interview_template
    return WorkflowRoute(
        workflow_id=workflow_id,
        deployment_id=deployment.deployment_id,
        version=version.version,
        content_hash=version.content_hash,
        template=_template_from_snapshot(snapshot),
        template_snapshot_json=snapshot.model_dump_json(),
        owner_subject=workflow.owner_subject,
    )


def session_template(row: Mapping[str, object]) -> InterviewTemplate:
    try:
        template_snapshot_json = row["template_snapshot_json"]
    except (KeyError, IndexError):
        template_snapshot_json = None

    if template_snapshot_json is None:
        return get_template(str(row["template_id"]))

    try:
        snapshot = TemplateSnapshot.model_validate_json(str(template_snapshot_json))
    except (ValidationError, ValueError) as exc:
        raise RuntimeError("stored workflow template snapshot is invalid") from exc
    return _template_from_snapshot(snapshot)


__all__ = [
    "WorkflowRoute",
    "WorkflowRoutingError",
    "resolve_workflow_route",
    "session_template",
]
