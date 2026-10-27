"""Workflow artifacts are immutable, validated, and content-addressed."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from pyinterviewbot_ai import workflow_models, workflow_tests
from pyinterviewbot_ai.templates import get_template
from pyinterviewbot_ai.workflow_models import (
    RuntimeRequirements,
    SecurityPolicy,
    WorkflowArtifact,
    WorkflowDraft,
    build_artifact,
    canonical_payload,
)


def _draft(**overrides) -> WorkflowDraft:
    values = {
        "name": "Backend reliability",
        "description": "A pinned backend interview workflow.",
        "template_id": "backend_engineer",
        "runtime": RuntimeRequirements(capabilities=["llm"]),
        "security": SecurityPolicy(),
    }
    values.update(overrides)
    return WorkflowDraft(**values)


def test_artifact_hash_is_deterministic_and_excludes_the_hash_field() -> None:
    workflow_id = uuid4()
    first = build_artifact(
        workflow_id=workflow_id,
        version=1,
        owner_subject="alice",
        draft=_draft(),
    )
    second = build_artifact(
        workflow_id=workflow_id,
        version=1,
        owner_subject="alice",
        draft=_draft(),
    )

    assert first.content_hash == second.content_hash
    assert first.verify_content_hash() is True
    assert canonical_payload(first)["content_hash"] is None

    tampered = first.model_copy(update={"content_hash": "0" * 64})
    assert tampered.verify_content_hash() is False


def test_artifact_hash_changes_when_identity_relevant_content_changes() -> None:
    workflow_id = uuid4()
    original = build_artifact(
        workflow_id=workflow_id,
        version=1,
        owner_subject="alice",
        draft=_draft(),
    )
    changed = build_artifact(
        workflow_id=workflow_id,
        version=1,
        owner_subject="alice",
        draft=_draft(description="A different pinned interview workflow."),
    )

    assert original.content_hash != changed.content_hash


def test_artifact_rejects_unknown_fields() -> None:
    artifact = build_artifact(
        workflow_id=uuid4(),
        version=1,
        owner_subject="alice",
        draft=_draft(),
    )
    payload = artifact.model_dump(mode="json")
    payload["unexpected"] = "nope"

    with pytest.raises(ValidationError):
        WorkflowArtifact.model_validate(payload)


def test_artifact_pins_the_interview_template_snapshot() -> None:
    artifact = build_artifact(
        workflow_id=uuid4(),
        version=1,
        owner_subject="alice",
        draft=_draft(),
    )

    snapshot = artifact.interview_template
    assert snapshot.id == "backend_engineer"
    assert snapshot.version == 1
    assert [node.id for node in snapshot.nodes] == [
        "failure_handling",
        "consistency",
        "incident_response",
    ]
    assert snapshot.max_turns == 6
    assert snapshot.system_prompt


def test_artifact_uses_the_template_own_version(monkeypatch) -> None:
    template = replace(get_template("backend_engineer"), version=7)
    monkeypatch.setattr(workflow_models, "get_template", lambda _: template)

    artifact = build_artifact(
        workflow_id=uuid4(),
        version=1,
        owner_subject="alice",
        draft=_draft(),
    )

    assert artifact.interview_template.version == 7


def test_template_pinning_rejects_a_changed_system_prompt(monkeypatch) -> None:
    artifact = build_artifact(
        workflow_id=uuid4(),
        version=1,
        owner_subject="alice",
        draft=_draft(),
    )
    template = get_template("backend_engineer")
    changed = replace(template, system_prompt=f"{template.system_prompt}\nChanged.")
    monkeypatch.setattr(workflow_tests, "get_template", lambda _: changed)

    result = workflow_tests._template_pinning(artifact)

    assert result["passed"] is False
    assert "stale" in result["detail"]


def test_template_pinning_rejects_a_changed_question_bank(monkeypatch) -> None:
    artifact = build_artifact(
        workflow_id=uuid4(),
        version=1,
        owner_subject="alice",
        draft=_draft(),
    )
    template = get_template("backend_engineer")
    changed = replace(template, question_bank=(*template.question_bank, "New question?"))
    monkeypatch.setattr(workflow_tests, "get_template", lambda _: changed)

    result = workflow_tests._template_pinning(artifact)

    assert result["passed"] is False
