"""Mandatory, deterministic sandbox suites for workflow artifacts."""

from __future__ import annotations

from .providers import EvidenceNodeSpec, TurnContext
from .providers.mock import MockLanguageModel
from .templates import get_template
from .workflow_env import EnvironmentBinding
from .workflow_models import WorkflowArtifact

MANDATORY_SUITES = (
    "schema_hash",
    "template_pinning",
    "security_policy",
    "sandbox_isolation",
    "mock_interview",
)


def _result(name: str, passed: bool, detail: str) -> dict:
    return {"name": name, "passed": passed, "detail": detail}


def _schema_hash(artifact: WorkflowArtifact) -> dict:
    valid = artifact.verify_content_hash() and bool(artifact.content_hash)
    return _result(
        "schema_hash",
        valid,
        "content hash matches canonical artifact"
        if valid
        else "artifact content hash does not match canonical content",
    )


def _template_pinning(artifact: WorkflowArtifact) -> dict:
    snapshot = artifact.interview_template
    try:
        current = get_template(snapshot.id)
    except ValueError:
        return _result("template_pinning", False, "pinned template no longer exists")
    valid = (
        snapshot.version == current.version
        and snapshot.title == current.title
        and snapshot.role == current.role
        and snapshot.competency == current.competency
        and snapshot.description == current.description
        and snapshot.max_turns == current.max_turns
        and snapshot.system_prompt == current.system_prompt
        and snapshot.evaluation_notes == current.evaluation_notes
        and snapshot.question_bank == current.question_bank
        and tuple((node.id, node.description) for node in snapshot.nodes)
        == tuple((node.id, node.description) for node in current.nodes)
    )
    return _result(
        "template_pinning",
        valid,
        "pinned template snapshot is complete and current"
        if valid
        else "pinned template snapshot is incomplete or stale",
    )


def _security_policy(artifact: WorkflowArtifact) -> dict:
    policy = artifact.security
    valid = (
        not policy.external_side_effects
        and not policy.allow_external_network
        and policy.redact_transcripts
        and policy.require_human_review
        and bool(artifact.runtime.capabilities)
    )
    return _result(
        "security_policy",
        valid,
        "policy forbids external side effects and requires human review"
        if valid
        else "security policy is missing a mandatory control",
    )


def _sandbox_isolation(
    artifact: WorkflowArtifact, binding: EnvironmentBinding
) -> dict:
    valid = (
        binding.environment == "sandbox"
        and binding.supports(artifact.runtime)
        and all(provider == "mock" for provider in binding.providers.values())
    )
    return _result(
        "sandbox_isolation",
        valid,
        "sandbox uses mock providers and satisfies runtime capabilities"
        if valid
        else "sandbox binding is not isolated or lacks a required capability",
    )


async def _mock_interview(artifact: WorkflowArtifact) -> dict:
    snapshot = artifact.interview_template
    model = MockLanguageModel()
    nodes = tuple(
        EvidenceNodeSpec(id=node.id, description=node.description)
        for node in snapshot.nodes
    )
    statuses: dict[str, str] = {}
    history: list[tuple[str, str]] = []
    evidence_updates = 0
    completed = False
    opening = ""

    for turn in range(snapshot.max_turns + 1):
        context = TurnContext(
            template_id=snapshot.id,
            competency=snapshot.competency,
            evidence_nodes=nodes,
            evidence_statuses=dict(statuses),
            question_bank=snapshot.question_bank,
            turn_number=turn,
            max_turns=snapshot.max_turns,
            force_conclude=turn >= snapshot.max_turns,
            latest_answer=history[-1][1] if history else "",
            history=tuple(history),
        )
        decision = await model.decide(snapshot.system_prompt, "", context)
        if turn == 0:
            opening = decision.question_text
            history.append(("ai", opening))
            if decision.is_complete or not opening:
                break
            continue
        for update in decision.evidence_updates:
            statuses[update.node] = update.status
            evidence_updates += 1
        if decision.is_complete:
            completed = True
            break
        answer = (
            "I personally designed the service, handled retries with an outbox, "
            "and verified the production result with concrete metrics."
        )
        history.append(("candidate", answer))
        history.append(("ai", decision.question_text))
        if not decision.question_text:
            break

    valid = bool(opening) and evidence_updates > 0 and completed
    return _result(
        "mock_interview",
        valid,
        "deterministic interview opened, collected evidence, and completed"
        if valid
        else "deterministic interview did not open, collect evidence, and complete",
    )


async def run_mandatory_suites(
    artifact: WorkflowArtifact, binding: EnvironmentBinding
) -> list[dict]:
    return [
        _schema_hash(artifact),
        _template_pinning(artifact),
        _security_policy(artifact),
        _sandbox_isolation(artifact, binding),
        await _mock_interview(artifact),
    ]


__all__ = ["MANDATORY_SUITES", "run_mandatory_suites"]
