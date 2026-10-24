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
    assessments: list[NodeAssessment] = []

    for node in template.nodes:
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
        if status == "not_addressed" or not citations:
            uncertainty = "high"
        elif status == "confirmed":
            uncertainty = "low"
        else:
            uncertainty = "high"
        priority, priority_weight = _priority_for(node.id, priority_map)
        assessments.append(
            NodeAssessment(
                node=node.id,
                description=node.description,
                status=status,
                evidence_strength=_STRENGTH.get(status),
                uncertainty=uncertainty,
                priority=priority,
                priority_weight=priority_weight,
                priority_status=_PRIORITY_STATUS.get(status, "not_demonstrated"),
                citations=citations,
                human_review=node_reviews,
            )
        )

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
    priority_gaps = [
        f"{item.node} ({item.priority}) is {item.priority_status}"
        for item in assessments
        if item.priority_status != "met"
    ]
    red_flags: list[RedFlag] = []
    for item in assessments:
        latest_citation = item.citations[-1] if item.citations else None
        turn = latest_citation.transcript_turn if latest_citation else None
        quote = latest_citation.quote if latest_citation else ""
        if item.status == "contradicted":
            red_flags.append(
                RedFlag(
                    code="contradicted_evidence",
                    severity="high",
                    node=item.node,
                    message="Candidate evidence conflicts with an earlier statement.",
                    evidence_turn=turn,
                    quote=quote,
                )
            )
        elif item.priority == "must_have" and item.status == "not_addressed":
            red_flags.append(
                RedFlag(
                    code="must_have_evidence_missing",
                    severity="high",
                    node=item.node,
                    message="A must-have rubric criterion has no observed evidence.",
                    evidence_turn=turn,
                    quote=quote,
                )
            )
        elif item.priority == "must_have" and item.status == "weak":
            red_flags.append(
                RedFlag(
                    code="must_have_evidence_weak",
                    severity="medium",
                    node=item.node,
                    message="A must-have criterion only has vague or generic evidence.",
                    evidence_turn=turn,
                    quote=quote,
                )
            )
        elif item.priority == "important" and item.status == "not_addressed":
            red_flags.append(
                RedFlag(
                    code="evidence_missing",
                    severity="medium",
                    node=item.node,
                    message="An important rubric criterion has no observed evidence.",
                    evidence_turn=turn,
                    quote=quote,
                )
            )
        for review in item.human_review:
            if review.decision == "reject":
                red_flags.append(
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
                red_flags.append(
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

    if coverage == 1.0 and contradictions == 0 and high_count == 0:
        overall = "low"
    elif coverage < 0.5 or contradictions > 0 or high_count > total / 2:
        overall = "high"
    else:
        overall = "medium"

    return SessionReport(
        session_id=session_id,
        template_id=template.id,
        competency=template.competency,
        node_assessments=assessments,
        evidence_coverage=round(coverage, 3),
        priority_weighted_coverage=weighted_coverage,
        priority_gaps=priority_gaps,
        red_flags=red_flags,
        red_flag_count=len(red_flags),
        contradictions=contradictions,
        overall_uncertainty=overall,
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
