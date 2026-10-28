"""Evidence-linked assessment report.

The report intentionally does not produce a hire/no-hire recommendation. It
maps rubric nodes to persisted transcript turns, labels evidence strength,
separates transcript quotes from model-written notes, and surfaces uncertainty
plus human review state.
"""

from __future__ import annotations

from uuid import UUID

from .models import (
    EvidenceCitation,
    EvidenceRecord,
    NodeAssessment,
    PrioritySetting,
    RedFlag,
    ReviewCorrection,
    SessionReport,
    TranscriptSegment,
)
from .providers.base import EvidenceNodeSpec
from .templates import InterviewTemplate

_STRENGTH = {"confirmed": 3, "weak": 1, "contradicted": 0}
_DEFAULT_WEIGHT = {"must_have": 3, "important": 2, "nice_to_have": 1}
_PRIORITY_STATUS = {
    "confirmed": "met",
    "weak": "partial",
    "contradicted": "not_demonstrated",
    "not_addressed": "not_demonstrated",
}


def _priority_for(
    node: str, priorities: dict[str, PrioritySetting]
) -> tuple[str, int]:
    setting = priorities.get(node)
    if setting is None:
        return "important", 2
    return setting.priority, setting.weight or _DEFAULT_WEIGHT[setting.priority]


def _quote_for_turn(
    transcript: list[TranscriptSegment], turn_number: int
) -> str:
    for segment in transcript:
        if segment.turn_number == turn_number and segment.speaker == "candidate":
            return segment.text[:500]
    return ""


def _uncertainty_for(status: str, citations: list[EvidenceCitation]) -> str:
    if status == "not_addressed" or not citations:
        return "high"
    if status == "confirmed":
        return "low"
    return "high"


def _assess_node(
    node: EvidenceNodeSpec,
    transcript: list[TranscriptSegment],
    evidence: list[EvidenceRecord],
    reviews: list[ReviewCorrection],
    priority_map: dict[str, PrioritySetting],
) -> NodeAssessment:
    records = [record for record in evidence if record.node == node.id]
    latest = records[-1] if records else None
    status = latest.status if latest else "not_addressed"
    node_reviews = [review for review in reviews if review.node == node.id]
    latest_decision = node_reviews[-1].decision if node_reviews else None
    citations = [
        EvidenceCitation(
            node=record.node,
            status=record.status,
            transcript_turn=record.turn_number,
            quote=_quote_for_turn(transcript, record.turn_number),
            note=record.note,
            human_decision=latest_decision,
        )
        for record in records
    ]
    priority, priority_weight = _priority_for(node.id, priority_map)
    return NodeAssessment(
        node=node.id,
        description=node.description,
        status=status,
        evidence_strength=_STRENGTH.get(status),
        uncertainty=_uncertainty_for(status, citations),
        priority=priority,
        priority_weight=priority_weight,
        priority_status=_PRIORITY_STATUS.get(status, "not_demonstrated"),
        citations=citations,
        human_review=node_reviews,
    )


def _node_red_flags(item: NodeAssessment) -> list[RedFlag]:
    latest_citation = item.citations[-1] if item.citations else None
    turn = latest_citation.transcript_turn if latest_citation else None
    quote = latest_citation.quote if latest_citation else ""
    if item.status == "contradicted":
        return [
            RedFlag(
                code="contradicted_evidence",
                severity="high",
                node=item.node,
                message="Candidate evidence conflicts with an earlier statement.",
                evidence_turn=turn,
                quote=quote,
            )
        ]
    if item.priority == "must_have" and item.status == "not_addressed":
        return [
            RedFlag(
                code="must_have_evidence_missing",
                severity="high",
                node=item.node,
                message="A must-have rubric criterion has no observed evidence.",
                evidence_turn=turn,
                quote=quote,
            )
        ]
    if item.priority == "must_have" and item.status == "weak":
        return [
            RedFlag(
                code="must_have_evidence_weak",
                severity="medium",
                node=item.node,
                message="A must-have criterion only has vague or generic evidence.",
                evidence_turn=turn,
                quote=quote,
            )
        ]
    if item.priority == "important" and item.status == "not_addressed":
        return [
            RedFlag(
                code="evidence_missing",
                severity="medium",
                node=item.node,
                message="An important rubric criterion has no observed evidence.",
                evidence_turn=turn,
                quote=quote,
            )
        ]
    return []


def _review_red_flags(item: NodeAssessment) -> list[RedFlag]:
    latest_citation = item.citations[-1] if item.citations else None
    turn = latest_citation.transcript_turn if latest_citation else None
    quote = latest_citation.quote if latest_citation else ""
    flags: list[RedFlag] = []
    for review in item.human_review:
        if review.decision == "reject":
            flags.append(
                RedFlag(
                    code="human_rejected_evidence",
                    severity="high",
                    node=item.node,
                    message="A human reviewer rejected this evidence.",
                    evidence_turn=turn,
                    quote=quote,
                    source="human_review",
                )
            )
        elif review.decision == "needs_more_evidence":
            flags.append(
                RedFlag(
                    code="human_requested_more_evidence",
                    severity="medium",
                    node=item.node,
                    message="A human reviewer requested more evidence.",
                    evidence_turn=turn,
                    quote=quote,
                    source="human_review",
                )
            )
    return flags


def _build_red_flags(
    assessments: list[NodeAssessment],
    transcript: list[TranscriptSegment],
    is_complete: bool | None,
) -> list[RedFlag]:
    red_flags = [
        flag
        for item in assessments
        for flag in (_node_red_flags(item) + _review_red_flags(item))
    ]
    if not transcript:
        red_flags.append(
            RedFlag(
                code="no_candidate_evidence",
                severity="high",
                message="No transcript evidence is available for this session.",
                source="session",
            )
        )
    if is_complete is False and transcript:
        red_flags.append(
            RedFlag(
                code="interview_incomplete",
                severity="low",
                message="The interview has not completed; findings are provisional.",
                source="session",
            )
        )
    return red_flags


def _priority_gaps(assessments: list[NodeAssessment]) -> list[str]:
    return [
        f"{item.node} ({item.priority}) is {item.priority_status}"
        for item in assessments
        if item.priority_status != "met"
    ]


def _overall_uncertainty(
    coverage: float,
    contradictions: int,
    high_count: int,
    total: int,
) -> str:
    if coverage == 1.0 and contradictions == 0 and high_count == 0:
        return "low"
    if coverage < 0.5 or contradictions > 0 or high_count > total / 2:
        return "high"
    return "medium"


def build_report(
    session_id: UUID,
    template: InterviewTemplate,
    transcript: list[TranscriptSegment],
    evidence: list[EvidenceRecord],
    reviews: list[ReviewCorrection] | None = None,
    priorities: list[PrioritySetting] | None = None,
    is_complete: bool | None = None,
) -> SessionReport:
    reviews = reviews or []
    priority_map = {item.node: item for item in (priorities or [])}
    assessments = [
        _assess_node(node, transcript, evidence, reviews, priority_map)
        for node in template.nodes
    ]

    total = len(assessments) or 1
    addressed = sum(1 for item in assessments if item.status != "not_addressed")
    coverage = addressed / total
    contradictions = sum(1 for item in assessments if item.status == "contradicted")
    high_count = sum(1 for item in assessments if item.uncertainty == "high")
    total_weight = sum(item.priority_weight for item in assessments) or 1
    addressed_weight = sum(
        item.priority_weight
        for item in assessments
        if item.status != "not_addressed"
    )
    weighted_coverage = round(addressed_weight / total_weight, 3)
    red_flags = _build_red_flags(assessments, transcript, is_complete)

    return SessionReport(
        session_id=session_id,
        template_id=template.id,
        competency=template.competency,
        node_assessments=assessments,
        evidence_coverage=round(coverage, 3),
        priority_weighted_coverage=weighted_coverage,
        priority_gaps=_priority_gaps(assessments),
        red_flags=red_flags,
        red_flag_count=len(red_flags),
        contradictions=contradictions,
        overall_uncertainty=_overall_uncertainty(
            coverage, contradictions, high_count, total
        ),
        limitations=[
            "Evidence strength is a rubric label, not a hiring recommendation.",
            "Model-written notes are inference; transcript quotes are the observed evidence.",
            (
                "Accent, vocal attractiveness, emotion, personality, protected characteristics, "
                "and unrelated communication style are not evaluated."
            ),
            (
                "Interview-flow audio is not persisted by this deployment; reports are "
                "transcript-based. The native studio recording API is explicit and in-memory."
            ),
            "Red flags are evidence/process signals, not findings of dishonesty or unsuitability.",
            "A human reviewer must confirm or reject evidence before any employment decision.",
        ],
    )


__all__ = ["build_report"]
