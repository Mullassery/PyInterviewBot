"""Immutable workflow artifacts and their canonical content hash.

An artifact is the deployable identity. Environment bindings, credentials,
test evidence, and deployment state deliberately live outside it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .templates import get_template

WORKFLOW_SCHEMA_VERSION = 1
ProviderCapability = Literal["llm", "asr", "tts"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TemplateNodeSnapshot(_StrictModel):
    id: str
    description: str


class TemplateSnapshot(_StrictModel):
    id: str
    version: int = Field(ge=1)
    title: str
    role: str
    competency: str
    description: str
    nodes: tuple[TemplateNodeSnapshot, ...]
    question_bank: tuple[str, ...]
    max_turns: int = Field(ge=1)
    system_prompt: str
    evaluation_notes: str


class RuntimeRequirements(_StrictModel):
    capabilities: tuple[ProviderCapability, ...] = ("llm",)


class SecurityPolicy(_StrictModel):
    external_side_effects: bool = False
    allow_external_network: bool = False
    redact_transcripts: bool = True
    require_human_review: bool = True


class WorkflowDraft(_StrictModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2_000)
    template_id: str = Field(min_length=1, max_length=120)
    runtime: RuntimeRequirements = Field(default_factory=RuntimeRequirements)
    security: SecurityPolicy = Field(default_factory=SecurityPolicy)


class WorkflowArtifact(_StrictModel):
    schema_version: Literal[1] = WORKFLOW_SCHEMA_VERSION
    workflow_id: UUID
    version: int = Field(ge=1)
    owner_subject: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2_000)
    interview_template: TemplateSnapshot
    runtime: RuntimeRequirements
    security: SecurityPolicy
    content_hash: str | None = None

    def verify_content_hash(self) -> bool:
        expected = content_hash_for(self)
        return bool(self.content_hash) and hmac.compare_digest(
            self.content_hash or "", expected
        )


def _template_snapshot(template_id: str) -> TemplateSnapshot:
    template = get_template(template_id)
    return TemplateSnapshot(
        id=template.id,
        version=template.version,
        title=template.title,
        role=template.role,
        competency=template.competency,
        description=template.description,
        nodes=tuple(
            TemplateNodeSnapshot(id=node.id, description=node.description)
            for node in template.nodes
        ),
        question_bank=template.question_bank,
        max_turns=template.max_turns,
        system_prompt=template.system_prompt,
        evaluation_notes=template.evaluation_notes,
    )


def canonical_payload(artifact: WorkflowArtifact) -> dict:
    payload = artifact.model_dump(mode="json")
    payload["content_hash"] = None
    return payload


def canonical_json(artifact: WorkflowArtifact) -> str:
    return json.dumps(
        canonical_payload(artifact),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def content_hash_for(artifact: WorkflowArtifact) -> str:
    return hashlib.sha256(canonical_json(artifact).encode("utf-8")).hexdigest()


def build_artifact(
    *,
    workflow_id: UUID,
    version: int,
    owner_subject: str,
    draft: WorkflowDraft,
) -> WorkflowArtifact:
    artifact = WorkflowArtifact(
        workflow_id=workflow_id,
        version=version,
        owner_subject=owner_subject,
        name=draft.name,
        description=draft.description,
        interview_template=_template_snapshot(draft.template_id),
        runtime=draft.runtime,
        security=draft.security,
    )
    return artifact.model_copy(update={"content_hash": content_hash_for(artifact)})


def artifact_from_json(value: str) -> WorkflowArtifact:
    return WorkflowArtifact.model_validate_json(value)


__all__ = [
    "WORKFLOW_SCHEMA_VERSION",
    "RuntimeRequirements",
    "SecurityPolicy",
    "TemplateNodeSnapshot",
    "TemplateSnapshot",
    "WorkflowArtifact",
    "WorkflowDraft",
    "artifact_from_json",
    "build_artifact",
    "canonical_json",
    "canonical_payload",
    "content_hash_for",
]
