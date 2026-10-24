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
    ReviewCorrection,
    SessionReport,
    TranscriptSegment,
)
from .templates import InterviewTemplate

_STRENGTH = {"confirmed": 3, "weak": 1, "contradicted": 0}


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
) -> SessionReport:
    reviews = reviews or []
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
        assessments.append(
            NodeAssessment(
                node=node.id,
                description=node.description,
                status=status,
                evidence_strength=_STRENGTH.get(status),
                uncertainty=uncertainty,
                citations=citations,
                human_review=node_reviews,
            )
        )

    total = len(assessments) or 1
    addressed = sum(1 for item in assessments if item.status != "not_addressed")
    coverage = addressed / total
    contradictions = sum(1 for item in assessments if item.status == "contradicted")
    high_count = sum(1 for item in assessments if item.uncertainty == "high")

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
        contradictions=contradictions,
        overall_uncertainty=overall,
        limitations=[
            "Evidence strength is a rubric label, not a hiring recommendation.",
            "Model-written notes are inference; transcript quotes are the observed evidence.",
            (
                "Accent, vocal attractiveness, emotion, personality, protected characteristics, "
                "and unrelated communication style are not evaluated."
            ),
            "Raw audio is not persisted by this deployment; reports are transcript-based.",
            "A human reviewer must confirm or reject evidence before any employment decision.",
        ],
    )


__all__ = ["build_report"]
